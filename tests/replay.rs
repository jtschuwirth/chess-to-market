//! Replay reads the fixture back in order, with the right variants and untouched timestamps.

use std::path::Path;

use eval_lag::sources::replay;
use eval_lag::Payload;

fn source_tag(p: &Payload) -> &'static str {
    match p {
        Payload::Ply { .. } => "ply",
        Payload::Market { .. } => "market",
        Payload::Eval { .. } => "eval",
        Payload::Note { .. } => "note",
    }
}

#[tokio::test]
async fn fixture_replays_in_order() {
    let records = replay::read_file(Path::new("fixtures/sample_run.jsonl"))
        .await
        .expect("fixture should parse");

    assert_eq!(records.len(), 12, "fixture record count");

    let tags: Vec<_> = records.iter().map(|o| source_tag(&o.payload)).collect();
    assert_eq!(
        tags,
        vec![
            "note", "market", "ply", "eval", "ply", "eval", "market", "ply", "eval", "market",
            "note", "eval"
        ]
    );

    // Monotonic stamps must be non-decreasing, or any duration computed from them is meaningless.
    for pair in records.windows(2) {
        assert!(
            pair[0].mono_ms <= pair[1].mono_ms,
            "mono_ms went backwards: {} then {}",
            pair[0].mono_ms,
            pair[1].mono_ms
        );
    }

    // Replay must pass recorded timestamps through untouched rather than re-stamping them.
    assert_eq!(records[0].mono_ms, 0);
    assert_eq!(records[11].mono_ms, 22500);
    assert_eq!(records[1].wall_unix_ms, 1_764_000_001_200);
}

#[tokio::test]
async fn mate_and_centipawn_scores_stay_distinct() {
    let records = replay::read_file(Path::new("fixtures/sample_run.jsonl"))
        .await
        .unwrap();

    let evals: Vec<_> = records
        .iter()
        .filter_map(|o| match &o.payload {
            Payload::Eval {
                eval_cp_white,
                mate_in,
                ..
            } => Some((*eval_cp_white, *mate_in)),
            _ => None,
        })
        .collect();

    assert_eq!(evals.len(), 4);
    // A mate score carries no centipawn value, and vice versa. Collapsing the two would corrupt
    // every evaluation delta that spans the mate.
    for (cp, mate) in &evals {
        assert!(
            cp.is_some() != mate.is_some(),
            "exactly one of cp/mate must be set, got {cp:?}/{mate:?}"
        );
    }
    assert_eq!(evals[3], (None, Some(5)));
}

#[tokio::test]
async fn malformed_line_is_an_error_naming_the_line() {
    let dir = std::env::temp_dir().join("eval-lag-test");
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("broken.jsonl");
    std::fs::write(
        &path,
        "{\"mono_ms\":0,\"wall_unix_ms\":0,\"payload\":{\"source\":\"note\",\"message\":\"ok\"}}\n\nnot json\n",
    )
    .unwrap();

    let err = replay::read_file(&path).await.expect_err("should fail");
    let msg = format!("{err:#}");
    assert!(msg.contains(":3:"), "error should name line 3, got: {msg}");
}
