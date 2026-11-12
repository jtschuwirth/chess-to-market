//! Where observations go: one JSON object per line, appended.

use std::path::Path;

use anyhow::{Context, Result};
use tokio::fs::{self, File, OpenOptions};
use tokio::io::{AsyncWriteExt, BufWriter};

use crate::observation::Observation;

/// Append-only JSONL writer.
///
/// # Why JSONL and not a JSON document or a database
///
/// A single JSON document has to be closed to be valid, so a run killed mid-game would leave an
/// unparseable file. JSONL is valid after every line, which means an interrupted run still yields
/// usable data — and the one run that matters here happens during a live game that will not be
/// repeated.
///
/// A database would be a second thing to install, configure and explain, for data that is written
/// once and read by a Python script. It would also be out of scope (plan §3).
pub struct JsonlSink {
    writer: BufWriter<File>,
    written: u64,
}

impl JsonlSink {
    /// Open (creating parent directories) and append.
    pub async fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)
                    .await
                    .with_context(|| format!("creating {}", parent.display()))?;
            }
        }
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .await
            .with_context(|| format!("opening {}", path.display()))?;
        Ok(Self {
            writer: BufWriter::new(file),
            written: 0,
        })
    }

    /// Write one record and flush it.
    ///
    /// Flushing per record costs a syscall we could batch. It is the right trade here: the volume is
    /// a few hundred records an hour, and the alternative is losing the tail of a world championship
    /// game to a buffer that never got flushed. Throughput is not a constraint on this program;
    /// not losing data is.
    pub async fn write(&mut self, obs: &Observation) -> Result<()> {
        let line = serde_json::to_string(obs).context("serialising observation")?;
        self.writer.write_all(line.as_bytes()).await?;
        self.writer.write_all(b"\n").await?;
        self.writer.flush().await?;
        self.written += 1;
        Ok(())
    }

    pub fn written(&self) -> u64 {
        self.written
    }

    /// Flush anything outstanding. Called on shutdown.
    pub async fn close(mut self) -> Result<u64> {
        self.writer.flush().await?;
        Ok(self.written)
    }
}
