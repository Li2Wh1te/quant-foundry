use qf_core::results::file::*;
use qf_core::results::*;
use qf_core::run::RunOutcome;
use qf_core::types::*;
use qf_core::{ErrorCode, QfError};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
fn sink(limits: FileLimits) -> FileResultSink {
    FileResultSink::create(
        &std::env::temp_dir(),
        "fixture",
        limits,
        Arc::new(AtomicBool::new(false)),
    )
    .unwrap()
}
fn log(s: &str) -> ResultRecord {
    ResultRecord::Log(LogRecord {
        time_ns: Nanoseconds::new(123),
        level: LogLevel::Info,
        message: s.into(),
        truncated: false,
    })
}
fn send(s: &mut FileResultSink, n: u64, records: Vec<ResultRecord>) -> Result<(), QfError> {
    let credit = s.credit()?;
    s.write(&ResultBatch::new(Sequence::new(n), records, &credit)?)
}
fn success() -> RunOutcome {
    RunOutcome::Succeeded {
        result_id: "fixture".into(),
    }
}
#[test]
fn bounded_shards_digests_pages_kind_and_run_bound_cursors() {
    let mut s = sink(FileLimits {
        shard_bytes: 512,
        require_summary: false,
        ..FileLimits::default()
    });
    for i in 0..20 {
        send(&mut s, i + 1, vec![log(&"x".repeat(90))]).unwrap();
    }
    let first = s.page("logs", None, 3).unwrap();
    assert_eq!(first["items"].as_array().unwrap().len(), 3);
    assert_eq!(first["partial"], true);
    let cursor = first["next_cursor"].as_str().unwrap();
    assert!(s.page("trades", Some(cursor), 3).is_err());
    let mut other = sink(FileLimits::default());
    assert!(other.page("logs", Some(cursor), 3).is_err());
    s.finalize(&success()).unwrap();
    let m = s.manifest().unwrap();
    assert!(m.shards.len() > 1);
    assert!(!m.partial);
    for shard in m.shards {
        assert_eq!(shard.sha256.len(), 64);
        assert_eq!(shard.index_bytes.get(), shard.count.get() * 16);
    }
    let second = s.page("logs", Some(cursor), 3).unwrap();
    assert_eq!(second["items"][0]["result_sequence"], "4");
    assert_eq!(second["partial"], false);
    assert!(s.page("../outside", None, 1).is_err());
    assert!(s.page("logs", None, 0).is_err());
    assert!(s.page("logs", None, 10001).is_err());
}
#[test]
fn required_capacity_and_summary_commit_fail_closed() {
    let mut s = sink(FileLimits {
        total_bytes: 1,
        ..FileLimits::default()
    });
    assert!(send(&mut s, 1, vec![log("hi")]).is_err());
    assert!(s.finalize(&success()).is_err());
    let failure = QfError::new(ErrorCode::ResultBudgetExceeded, "fixture", "full");
    let _ = s.abort(&failure);
    assert!(matches!(s.outcome(), Some(RunOutcome::Failed { .. })));
    let mut s = sink(FileLimits::default());
    send(&mut s, 1, vec![log("hi")]).unwrap();
    assert!(s.finalize(&success()).is_err());
    assert!(s.retain().is_err());
}
#[test]
fn logarithmic_and_empty_message_flood_has_visible_bounded_truncation() {
    let mut s = sink(FileLimits {
        log_records: 2,
        log_bytes: 7,
        require_summary: false,
        ..FileLimits::default()
    });
    send(&mut s, 1, vec![log("中文中文"), log(""), log("discard")]).unwrap();
    for i in 4..10004 {
        send(&mut s, i, vec![log("")]).unwrap();
    }
    s.finalize(&success()).unwrap();
    let m = s.manifest().unwrap();
    assert_eq!(m.counts["logs"].get(), 2);
    assert_eq!(m.notices.logs_dropped.get(), 10001);
    assert!(m.notices.logs_truncated);
    let page = s.page("logs", None, 10).unwrap();
    assert_eq!(page["items"][0]["message"], "中文");
    assert_eq!(page["items"][0]["truncated"], true);
}
#[test]
fn zero_credit_sequence_gaps_and_cancel_do_not_succeed() {
    let mut s = sink(FileLimits {
        require_summary: false,
        ..FileLimits::default()
    });
    assert!(send(&mut s, 2, vec![log("hi")]).is_err());
    assert!(s.finalize(&success()).is_err());
    assert!(
        ResultBatch::new(
            Sequence::new(1),
            vec![log("hi")],
            &SinkCredit {
                max_records: 0,
                max_bytes: 100
            }
        )
        .is_err()
    );
    let cancel = Arc::new(AtomicBool::new(false));
    let mut s = FileResultSink::create(
        &std::env::temp_dir(),
        "fixture",
        FileLimits {
            require_summary: false,
            ..FileLimits::default()
        },
        cancel.clone(),
    )
    .unwrap();
    send(&mut s, 1, vec![log("hi")]).unwrap();
    cancel.store(true, Ordering::Release);
    assert_eq!(
        s.finalize(&success()).unwrap_err().code,
        ErrorCode::Cancelled
    );
    let _ = s.abort(&QfError::new(ErrorCode::Cancelled, "fixture", "cancel"));
    assert!(matches!(s.outcome(), Some(RunOutcome::Cancelled { .. })));
}
#[test]
fn symlink_root_replacement_and_sealed_corruption_are_rejected() {
    use std::os::unix::fs::symlink;
    let mut s = sink(FileLimits {
        shard_bytes: 512,
        require_summary: false,
        ..FileLimits::default()
    });
    for i in 0..4 {
        send(&mut s, i + 1, vec![log(&"x".repeat(90))]).unwrap();
    }
    let root = std::env::temp_dir().join(s.directory_name());
    let data = std::fs::read_dir(&root)
        .unwrap()
        .map(|f| f.unwrap().path())
        .find(|p| p.extension().is_some_and(|e| e == "data"))
        .unwrap();
    std::fs::write(&data, b"corrupt").unwrap();
    assert!(s.finalize(&success()).is_err());
    assert!(!root.join("manifest.json").exists());
    let mut s = sink(FileLimits {
        require_summary: false,
        ..FileLimits::default()
    });
    send(&mut s, 1, vec![log("hi")]).unwrap();
    let root = std::env::temp_dir().join(s.directory_name());
    let moved = root.with_extension("moved");
    std::fs::rename(&root, &moved).unwrap();
    symlink(&moved, &root).unwrap();
    assert!(s.finalize(&success()).is_err());
    drop(s);
    assert!(root.symlink_metadata().is_ok());
    std::fs::remove_file(root).unwrap();
    std::fs::remove_dir(moved).unwrap();
    let path = std::env::temp_dir().join(format!("qf-symlink-parent-{}", std::process::id()));
    symlink(std::env::temp_dir(), &path).unwrap();
    assert!(
        FileResultSink::create(
            &path,
            "fixture",
            FileLimits::default(),
            Arc::new(AtomicBool::new(false))
        )
        .is_err()
    );
    std::fs::remove_file(path).unwrap();
}
#[test]
fn blocked_credit_close_and_cancellation_interrupt_without_unbounded_queue() {
    use qf_core::results::credit::CreditGate;
    use std::time::{Duration, Instant};
    let gate = Arc::new(CreditGate::default());
    let cancel = Arc::new(AtomicBool::new(false));
    let g = gate.clone();
    let c = cancel.clone();
    let thread = std::thread::spawn(move || g.wait(&c, Instant::now() + Duration::from_secs(2)));
    cancel.store(true, Ordering::Release);
    assert_eq!(
        thread.join().unwrap().unwrap_err().code,
        ErrorCode::Cancelled
    );
    let gate = Arc::new(CreditGate::default());
    let g = gate.clone();
    let thread = std::thread::spawn(move || {
        g.wait(
            &AtomicBool::new(false),
            Instant::now() + Duration::from_secs(2),
        )
    });
    gate.close();
    assert!(thread.join().unwrap().is_err());
    let gate = CreditGate::default();
    gate.grant(SinkCredit {
        max_records: 1,
        max_bytes: 100,
    })
    .unwrap();
    assert!(
        gate.grant(SinkCredit {
            max_records: 1,
            max_bytes: 100
        })
        .is_err()
    );
    assert_eq!(
        gate.wait(
            &AtomicBool::new(false),
            Instant::now() + Duration::from_secs(1)
        )
        .unwrap()
        .max_records,
        1
    );
}
#[test]
fn retained_reader_reopens_bounded_history_and_detects_damage() {
    let mut sink = sink(FileLimits {
        require_summary: false,
        shard_bytes: 512,
        ..FileLimits::default()
    });
    for i in 0..10 {
        send(&mut sink, i + 1, vec![log("history")]).unwrap();
    }
    sink.finalize(&success()).unwrap();
    let name = sink.directory_name().to_owned();
    sink.retain().unwrap();
    drop(sink);
    let mut reader = FileResultReader::open(&std::env::temp_dir(), &name, "fixture").unwrap();
    let first = reader.page(ResultKind::Logs, None, 2).unwrap();
    assert_eq!(first["items"].as_array().unwrap().len(), 2);
    assert_eq!(
        reader
            .page(ResultKind::Logs, first["next_cursor"].as_str(), 2)
            .unwrap()["items"][0]["result_sequence"],
        "3"
    );
    assert!(FileResultReader::open(&std::env::temp_dir(), &name, "wrong-run").is_err());
    let root = std::env::temp_dir().join(&name);
    let data = std::fs::read_dir(&root)
        .unwrap()
        .map(|f| f.unwrap().path())
        .find(|p| p.extension().is_some_and(|e| e == "data"))
        .unwrap();
    std::fs::write(data, b"damage").unwrap();
    assert!(reader.page(ResultKind::Logs, None, 100).is_err());
    drop(reader);
    for path in std::fs::read_dir(&root).unwrap() {
        std::fs::remove_file(path.unwrap().path()).unwrap();
    }
    std::fs::remove_dir(root).unwrap();
}
#[test]
fn final_dependency_guard_refuses_success_and_ipc_rejects_unknown_contracts() {
    let mut s = sink(FileLimits {
        require_summary: false,
        ..FileLimits::default()
    });
    send(&mut s, 1, vec![log("diagnostic")]).unwrap();
    let error = s
        .finalize_guarded(&success(), || {
            Err::<(), _>(QfError::new(ErrorCode::DataChanged, "check", "changed"))
        })
        .unwrap_err();
    assert_eq!(error.code, ErrorCode::DataChanged);
    assert!(s.outcome().is_none());
    let root = std::env::temp_dir().join(s.directory_name());
    assert!(!root.join("manifest.json").exists());
    s.abort(&error).unwrap();
    assert!(matches!(s.outcome(), Some(RunOutcome::Failed { .. })));
    let credit = SinkCredit {
        max_records: 1,
        max_bytes: 1024,
    };
    for bytes in [
        br#"{"first_sequence":"1","records":[{"kind":"market_event","record":{}}]}"#.as_slice(),
        br#"{"first_sequence":"1","records":[],"unknown":1}"#.as_slice(),
    ] {
        assert_eq!(
            ResultBatch::decode(bytes, &credit).unwrap_err().code,
            ErrorCode::InvalidContract
        );
    }
}
#[test]
fn shard_symlink_and_hard_link_do_not_write_outside_owned_files() {
    use std::os::unix::fs::symlink;
    for hard in [false, true] {
        let mut s = sink(FileLimits {
            require_summary: false,
            ..FileLimits::default()
        });
        send(&mut s, 1, vec![log("first")]).unwrap();
        let root = std::env::temp_dir().join(s.directory_name());
        let data = root.join("logs-000000.data");
        let outside = root.with_extension(if hard {
            "hard-fixture"
        } else {
            "outside-fixture"
        });
        if hard {
            std::fs::hard_link(&data, &outside).unwrap();
        } else {
            std::fs::write(&outside, b"protected fixture").unwrap();
            std::fs::remove_file(&data).unwrap();
            symlink(&outside, &data).unwrap();
        }
        let prior = std::fs::read(&outside).unwrap();
        assert!(send(&mut s, 2, vec![log("second")]).is_err());
        assert_eq!(std::fs::read(&outside).unwrap(), prior);
        assert!(s.finalize(&success()).is_err());
        drop(s);
        std::fs::remove_file(outside).unwrap();
    }
}

#[test]
fn manifest_swap_existing_target_and_commit_cancellation_refuse_success() {
    for pending_swap in [true, false] {
        let mut s = sink(FileLimits {
            require_summary: false,
            ..FileLimits::default()
        });
        send(&mut s, 1, vec![log("required diagnostic")]).unwrap();
        let root = std::env::temp_dir().join(s.directory_name());
        assert!(
            s.finalize_guarded(&success(), || {
                if pending_swap {
                    std::fs::remove_file(root.join("manifest.pending")).unwrap();
                    std::fs::write(root.join("manifest.pending"), b"replacement").unwrap();
                } else {
                    std::fs::write(root.join("manifest.json"), b"unowned target").unwrap();
                }
                Ok(())
            })
            .is_err()
        );
        assert!(s.outcome().is_none());
        if !pending_swap {
            assert_eq!(
                std::fs::read(root.join("manifest.json")).unwrap(),
                b"unowned target"
            );
            std::fs::remove_file(root.join("manifest.json")).unwrap();
        }
    }
    let cancel = Arc::new(AtomicBool::new(false));
    let mut s = FileResultSink::create(
        &std::env::temp_dir(),
        "fixture",
        FileLimits {
            require_summary: false,
            ..FileLimits::default()
        },
        cancel.clone(),
    )
    .unwrap();
    send(&mut s, 1, vec![log("diagnostic")]).unwrap();
    assert_eq!(
        s.finalize_guarded(&success(), || {
            cancel.store(true, Ordering::Release);
            Ok(())
        })
        .unwrap_err()
        .code,
        ErrorCode::Cancelled
    );
    assert!(
        !std::env::temp_dir()
            .join(s.directory_name())
            .join("manifest.json")
            .exists()
    );
    let _ = s.abort(&QfError::new(ErrorCode::Cancelled, "fixture", "cancel"));
    assert!(matches!(s.outcome(), Some(RunOutcome::Cancelled { .. })));
}

#[test]
fn active_page_replacement_and_sealed_permission_change_are_rejected() {
    use std::os::unix::fs::PermissionsExt;
    let mut s = sink(FileLimits {
        require_summary: false,
        shard_bytes: 512,
        ..FileLimits::default()
    });
    send(&mut s, 1, vec![log("original")]).unwrap();
    let root = std::env::temp_dir().join(s.directory_name());
    let data = root.join("logs-000000.data");
    let original = std::fs::read(&data).unwrap();
    let old = root.join("old-owned-fixture");
    std::fs::rename(&data, &old).unwrap();
    std::fs::write(&data, &original).unwrap();
    std::fs::set_permissions(&data, std::fs::Permissions::from_mode(0o600)).unwrap();
    assert!(s.page("logs", None, 1).is_err());
    std::fs::remove_file(data).unwrap();
    std::fs::rename(old, root.join("logs-000000.data")).unwrap();
    for i in 2..6 {
        send(&mut s, i, vec![log(&"x".repeat(90))]).unwrap();
    }
    std::fs::set_permissions(
        root.join("logs-000000.data"),
        std::fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    assert!(s.finalize(&success()).is_err());
    assert!(!root.join("manifest.json").exists());
}
