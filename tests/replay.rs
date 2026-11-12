//! Replay reads the fixture back in order, with the right variants and untouched timestamps.
//!
//! The fixture is also a worked example of the wire format in `analysis/SCHEMA.md`, so these tests
//! double as a check that the Rust and Python halves still agree on it. `python -m analysis validate
//! fixtures/sample_run.jsonl` must pass alongside them.

use std::path::Path;

use chess_to_market::sources::replay;
use chess_to_market::Payload;

fn source_tag(p: &Payload) -> &'static str {
    match p {
        Payload::Ply { .. } => "ply",
        Payload::Market { .. } => "market",
        Payload::Eval { .. } => "eval",
        Payload::Signal { .. } => "signal",
        Payload::Note { .. } => "note",
    }
}

#[tokio::test]
async fn fixture_replays_in_order() {
    let records = replay::read_file(Path::new("fixtures/sample_run.jsonl"))
        .await
        .expect("fixture should parse");

    assert_eq!(records.len(), 14, "fixture record count");

    let tags: Vec<_> = records.iter().map(|o| source_tag(&o.payload)).collect();
    assert_eq!(
        tags,
        vec![
            "note", "market", "ply", "eval", "eval", "ply", "eval", "market", "ply", "eval",
            "signal", "market", "note", "eval"
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
    assert_eq!(records[13].mono_ms, 22500);
    assert_eq!(records[1].wall_unix_ms, 1_764_000_001_200);
}

#[tokio::test]
async fn evals_carry_wdl_and_multipv_lines() {
    // The two fields the whole analysis depends on: probability-space evaluation, and enough
    // candidate lines for the findability model to count holding moves.
    let records = replay::read_file(Path::new("fixtures/sample_run.jsonl"))
        .await
        .unwrap();

    let mut seen = 0;
    for o in &records {
        if let Payload::Eval {
            wdl_white, lines, ..
        } = &o.payload
        {
            seen += 1;
            let total: u32 = wdl_white.iter().map(|&x| x as u32).sum();
            assert!(
                (995..=1005).contains(&total),
                "wdl_white should sum to ~1000 per-mille, got {total}"
            );
            assert!(!lines.is_empty(), "eval should carry MultiPV lines");
            for l in lines {
                let t: u32 = l.wdl.iter().map(|&x| x as u32).sum();
                assert!((995..=1005).contains(&t), "line wdl should sum to ~1000, got {t}");
            }
        }
    }
    assert_eq!(seen, 5);
}

#[tokio::test]
async fn plies_carry_a_reconstructed_move_time() {
    // Reconstructed from the clock, so it precedes the record's own wall clock: the relay and the
    // engine both add delay after the move was actually played.
    let records = replay::read_file(Path::new("fixtures/sample_run.jsonl"))
        .await
        .unwrap();

    let mut seen = 0;
    for o in &records {
        if let Payload::Ply { move_unix_ms, .. } = &o.payload {
            seen += 1;
            assert!(
                *move_unix_ms <= o.wall_unix_ms,
                "move time {move_unix_ms} should not be after the record that reports it"
            );
        }
    }
    assert_eq!(seen, 3);
}

#[tokio::test]
async fn market_records_carry_an_operator_leg() {
    // Without a leg the analysis cannot tell which outcome an asset id is, and refuses to use it.
    let records = replay::read_file(Path::new("fixtures/sample_run.jsonl"))
        .await
        .unwrap();

    let mut seen = 0;
    for o in &records {
        if let Payload::Market { leg, .. } = &o.payload {
            seen += 1;
            assert!(!leg.is_empty());
        }
    }
    assert_eq!(seen, 3);
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
                cp_white, mate_in, ..
            } => Some((*cp_white, *mate_in)),
            _ => None,
        })
        .collect();

    assert_eq!(evals.len(), 5);
    // A mate score carries no centipawn value, and vice versa. Collapsing the two would corrupt
    // every evaluation delta that spans the mate.
    for (cp, mate) in &evals {
        assert!(
            cp.is_some() != mate.is_some(),
            "exactly one of cp/mate must be set, got {cp:?}/{mate:?}"
        );
    }
    assert_eq!(evals[4], (None, Some(-5)));
}

#[tokio::test]
async fn malformed_line_is_an_error_naming_the_line() {
    let dir = std::env::temp_dir().join("chess-to-market-test");
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
