//! Replay a recorded log back through the pipeline.
//!
//! This is both the test harness and the demo that works when no game is on.

use std::path::Path;

use anyhow::{Context, Result};
use tokio::fs::File;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::mpsc::Sender;

use crate::observation::Observation;

/// Read a JSONL log into memory.
///
/// Blank lines are skipped; a malformed line is an error naming its line number, because a replay
/// that silently drops records would make a test pass for the wrong reason.
pub async fn read_file(path: &Path) -> Result<Vec<Observation>> {
    let file = File::open(path)
        .await
        .with_context(|| format!("opening {}", path.display()))?;
    let mut lines = BufReader::new(file).lines();
    let mut out = Vec::new();
    let mut lineno = 0usize;

    while let Some(line) = lines.next_line().await? {
        lineno += 1;
        if line.trim().is_empty() {
            continue;
        }
        let obs: Observation = serde_json::from_str(&line)
            .with_context(|| format!("{}:{lineno}: malformed record", path.display()))?;
        out.push(obs);
    }
    Ok(out)
}

/// Run as a source: read the log and send every record on.
///
/// **Timestamps are passed through untouched.** Re-stamping on replay would destroy the very
/// intervals the log was recorded to preserve, which would make replayed runs useless for checking
/// the analysis.
///
/// There is deliberately no pacing: records go through as fast as they parse. If you want a demo
/// that unfolds in real time, pace it on the recorded `mono_ms` deltas — that is a nice-to-have,
/// not part of T-01.
pub async fn run(path: &Path, tx: Sender<Observation>) -> Result<usize> {
    let records = read_file(path).await?;
    let n = records.len();
    for obs in records {
        // A send error means the consumer is gone, which during shutdown is normal.
        if tx.send(obs).await.is_err() {
            break;
        }
    }
    Ok(n)
}
