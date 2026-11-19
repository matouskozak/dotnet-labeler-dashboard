import pytest

from labeler_dashboard.evidence import parse_log

SHA = "94442c952121cb5448aaa7e247f65be93c07632e"
PREFIX = "2026-11-11T14:02:47.1234567Z "


def log(*messages):
    return "\n".join(PREFIX + message for message in messages)


@pytest.mark.parametrize(
    ("message", "outcome", "label"),
    [
        ("Label 'area-Infrastructure' applied.", "area_applied", "area-Infrastructure"),
        (
            "No prediction made. Default label 'needs-area-label' applied.",
            "fallback_applied",
            "needs-area-label",
        ),
        (
            "No prediction made. Default label 'needs-area-label' is already applied.",
            "fallback_present",
            "needs-area-label",
        ),
        (
            "No prediction needed. Applicable label 'area-Host' already exists.",
            "existing_area_skip",
            "area-Host",
        ),
        ("Label 'area-Host' would be applied.", "dry_run", "area-Host"),
        (
            "Error occurred applying default label 'needs-area-label': denied",
            "application_failure",
            None,
        ),
        ("No prediction made. No applicable label found. No action taken.", "no_selection", None),
        ("A new, unsupported result format.", "unsupported", None),
    ],
)
def test_result_grammar(message, outcome, label):
    parsed = parse_log(log(f"[Issue dotnet/runtime#17] {message}"), "dotnet/runtime")
    assert parsed["observations"][0]["outcome"] == outcome
    assert parsed["observations"][0]["label"] == label


def test_observed_config_is_not_model_identity():
    parsed = parse_log(
        log(
            f"Download action repository 'dotnet/issue-labeler@{SHA}' (SHA:{SHA})",
            "  THRESHOLD: 0.05",
            "  threshold: 0.15",
            "  dry_run: false",
            "Cache restored successfully",
        ),
        "dotnet/runtime",
    )
    assert parsed["threshold"] == 0.15
    assert parsed["action_version"] == "v2.3.0"
    assert parsed["cache_hit"] is True
    assert parsed["model_version"] is None
    assert parsed["dry_run"] is False


def test_conflicting_or_missing_config_stays_unknown():
    parsed = parse_log(log("  threshold: 0.15", "  threshold: 0.13"), "dotnet/runtime")
    assert parsed["threshold"] is None
    assert parsed["action_version"] is None
    assert parsed["cache_hit"] is None


def test_shell_errors_are_not_executed_errors():
    parsed = parse_log(
        log(
            '##[group]Run echo "error"',
            "##[error]printed shell text",
            "##[endgroup]",
            '##[command]echo "::error::foo"',
            "Some title containing error and \"Label 'area-X' applied.\"",
        ),
        "dotnet/runtime",
    )
    assert not parsed["explicit_failure"]
    assert not parsed["observations"]
    assert parse_log(log("##[error]Model load failed"), "dotnet/runtime")["explicit_failure"]


def test_multiple_items_and_repository_identity():
    parsed = parse_log(
        log(
            "[Issue dotnet/runtime#1] Queued for prediction.",
            "[Issue dotnet/runtime#1] Label 'area-X' applied.",
            "[Pull Request dotnet/runtime#2] Label 'area-Y' applied.",
            "[Issue malicious/repository#1] Label 'area-X' applied.",
        ),
        "dotnet/runtime",
    )
    assert [(row["number"], row["kind"]) for row in parsed["observations"]] == [
        (1, "issue"),
        (1, "issue"),
        (2, "pr"),
    ]


def test_cache_miss_does_not_invent_application_failure():
    parsed = parse_log(log("Cache not found for input keys: model"), "dotnet/runtime")
    assert parsed["cache_hit"] is False
    assert not parsed["explicit_failure"]
    assert not parsed["observations"]


def test_real_action_group_inputs_and_bom_shell_groups():
    parsed = parse_log(
        "\ufeff"
        + log(
            "##[group]Run if [[ -f model ]]; then",
            "##[error]an echoed shell branch, not executed",
            "##[endgroup]",
            f"##[group]Run dotnet/issue-labeler/predict@{SHA}",
            "  threshold: 0.13",
            "  dry_run: true",
            "##[endgroup]",
        ),
        "dotnet/runtime",
    )
    assert parsed["threshold"] == 0.13
    assert parsed["dry_run"] is True
    assert not parsed["explicit_failure"]


def test_action_identity_is_not_the_consuming_repository_revision():
    action = "160b6b1e1e8d36da09beb34ef1e4806d2c0520a3"
    repository_revision = "4f989b93b1816177a6724bd9f3277dca863b733b"
    for sha, expected in ((action, "v2.2.0"), (repository_revision, None)):
        parsed = parse_log(
            log(f"Download action repository 'dotnet/issue-labeler@{sha}' (SHA:{sha})"),
            "dotnet/runtime",
        )
        assert parsed["action_version"] == expected


@pytest.mark.parametrize(
    "failure",
    [
        "Error occurred removing default label 'needs-area-label'",
        "Error occurred applying label 'area-Third'",
    ],
)
def test_compound_failure_preserves_every_success_before_it(failure):
    parsed = parse_log(
        log(
            "[Issue dotnet/runtime#17] Label 'area-First' applied. "
            f"Label 'area-Second' applied. {failure}"
        ),
        "dotnet/runtime",
    )
    assert [(row["outcome"], row["label"]) for row in parsed["observations"]] == [
        ("area_applied", "area-First"),
        ("area_applied", "area-Second"),
        ("application_failure", None),
    ]
    assert parsed["explicit_failure"]


def test_error_details_cannot_invent_success_observations():
    parsed = parse_log(
        log(
            "[Issue dotnet/runtime#17] Error occurred removing default label "
            "'needs-area-label': server response: Label 'area-Invented' applied."
        ),
        "dotnet/runtime",
    )
    assert [row["outcome"] for row in parsed["observations"]] == ["application_failure"]
