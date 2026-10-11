//! Trusted D12 writer reuses D11 shards; no second persistence format.
use crate::{convert, python_error};
use pyo3::prelude::*;
use qf_core::data::views::bounded_json;
use qf_core::results::file::{FileLimits, FileResultReader, FileResultSink};
use qf_core::results::{ResultBatch, ResultKind, ResultSink};
use qf_core::run::RunOutcome;
use qf_core::{QfError, QfResult};
use std::path::Path;
use std::sync::{Arc, atomic::AtomicBool};

#[pyclass]
pub struct TrustedResultWriter {
    sink: FileResultSink,
}
#[pymethods]
impl TrustedResultWriter {
    #[staticmethod]
    fn validate_outcome_json(input: &str) -> PyResult<String> {
        let outcome: RunOutcome = convert(bounded_json(input))?;
        convert(serde_json::to_string(&outcome).map_err(|_| invalid()))
    }
    #[new]
    fn new(parent: &str, run_id: &str, maximum: u64) -> PyResult<Self> {
        Ok(Self {
            sink: convert(FileResultSink::create(
                Path::new(parent),
                run_id,
                FileLimits {
                    total_bytes: maximum,
                    ..FileLimits::default()
                },
                Arc::new(AtomicBool::new(false)),
            ))?,
        })
    }
    fn write_json(&mut self, py: Python<'_>, input: &str) -> PyResult<()> {
        convert(py.detach(|| {
            let credit = self.sink.credit()?;
            let batch = ResultBatch::decode(input.as_bytes(), &credit)?;
            self.sink.write(&batch)
        }))
    }
    fn notices_json(&mut self, input: &str) -> PyResult<()> {
        convert(self.sink.merge_notices(convert(bounded_json(input))?))
    }
    /// Prepare a durable local candidate; only the repository publishes it.
    fn seal(&mut self, py: Python<'_>, input: &str) -> PyResult<()> {
        convert(py.detach(|| {
            let outcome: RunOutcome = bounded_json(input)?;
            match &outcome {
                RunOutcome::Succeeded { .. } => self.sink.finalize(&outcome),
                RunOutcome::Failed { error, .. } => self.sink.abort(error),
                RunOutcome::Cancelled { .. } => self.sink.abort(&QfError::new(
                    qf_core::ErrorCode::Cancelled,
                    "worker",
                    "运行已取消",
                )),
            }
        }))
    }
    fn retain(&mut self) -> PyResult<String> {
        convert(self.sink.retain())?;
        Ok(self.sink.directory_name().into())
    }
    fn directory_name(&self) -> String {
        self.sink.directory_name().into()
    }
    fn overview_json(&self) -> PyResult<String> {
        convert(serde_json::to_string(&self.sink.overview()).map_err(|_| invalid()))
    }
}
fn invalid() -> QfError {
    QfError::new(
        qf_core::ErrorCode::InvalidContract,
        "result_reader",
        "结果消息无效",
    )
}
#[pyclass]
pub struct RetainedResultReader {
    reader: FileResultReader,
}
#[pymethods]
impl RetainedResultReader {
    #[new]
    fn new(parent: &str, directory_name: &str, run_id: &str) -> PyResult<Self> {
        Ok(Self {
            reader: convert(FileResultReader::open(
                Path::new(parent),
                directory_name,
                run_id,
            ))?,
        })
    }
    fn summary_json(&self) -> PyResult<String> {
        convert(serde_json::to_string(&self.reader.overview()).map_err(|_| invalid()))
    }
    #[pyo3(signature=(kind, after=None, limit=100))]
    fn page_json(
        &mut self,
        py: Python<'_>,
        kind: &str,
        after: Option<&str>,
        limit: usize,
    ) -> PyResult<String> {
        let kind: ResultKind = convert(bounded_json(
            &serde_json::to_string(kind).map_err(|_| python_error(invalid()))?,
        ))?;
        convert(py.detach(|| -> QfResult<String> {
            serde_json::to_string(&self.reader.page(kind, after, limit)?).map_err(|_| invalid())
        }))
    }
}
pub fn register(m: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    m.add_class::<TrustedResultWriter>()?;
    m.add_class::<RetainedResultReader>()?;
    Ok(())
}
