//! Release gates for conversation-local selection, using real mock-server processes.

use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::time::Duration;

use codex_config::McpServerConfig;
use codex_core::StartThreadOptions;
use codex_core::TurnInputRequest;
use codex_mcp::McpSnapshotDetail;
use codex_protocol::protocol::EventMsg;
use codex_protocol::protocol::Op;
use codex_protocol::user_input::UserInput;
use core_test_support::responses;
use core_test_support::responses::ResponseMock;
use core_test_support::stdio_server_bin;
use core_test_support::test_codex::TestCodex;
use core_test_support::test_codex::test_codex;
use core_test_support::wait_for_event;
use pretty_assertions::assert_eq;
use serde_json::json;
use wiremock::MockServer;

fn server_config(dir: &Path, name: &str) -> anyhow::Result<McpServerConfig> {
    Ok(serde_json::from_value(json!({
        "command": stdio_server_bin()?, "enabled": false,
        "enabled_tools": ["echo", "sync_readonly"], "startup_timeout_sec": 2,
        "env": {
            "MCP_TEST_SPAWN_LOG": dir.join(format!("{name}.log")),
            "MCP_TEST_DYNAMIC_SERVER_METADATA": "1"
        }
    }))?)
}

fn spawns(dir: &Path, name: &str) -> Vec<String> {
    fs::read_to_string(dir.join(format!("{name}.log")))
        .unwrap_or_default()
        .lines()
        .map(str::to_string)
        .collect()
}

fn alive(pid: &str) -> bool {
    #[cfg(unix)]
    {
        core_test_support::process::process_is_alive(pid).expect("inspect mock server process")
    }
    #[cfg(windows)]
    {
        let output = std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH", "/FO", "CSV"])
            .output()
            .expect("inspect mock server process");
        String::from_utf8_lossy(&output.stdout).contains(&format!("\"{pid}\""))
    }
}

async fn stopped(pid: &str) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while alive(pid) {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("owned MCP process must stop on shutdown");
}

async fn fixture(
    server: &MockServer,
    servers: HashMap<String, McpServerConfig>,
) -> anyhow::Result<TestCodex> {
    test_codex()
        .with_model_info_override("gpt-5.4", |model| model.supports_search_tool = false)
        .with_config(move |config| {
            config
                .mcp_servers
                .set(servers)
                .expect("configure mock servers");
        })
        .build_with_auto_env(server)
        .await
}

async fn completion(server: &MockServer) -> ResponseMock {
    responses::mount_sse_once(
        server,
        responses::sse(vec![
            responses::ev_response_created("selection-response"),
            responses::ev_assistant_message("selection-message", "done"),
            responses::ev_completed("selection-response"),
        ]),
    )
    .await
}

fn advertised(response: &ResponseMock, name: &str) -> bool {
    responses::namespace_child_tool(
        &response.single_request().body_json(),
        &format!("mcp__{name}"),
        "echo",
    )
    .is_some()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_activation_retention_isolation_and_shutdown() -> anyhow::Result<()> {
    let model = responses::start_mock_server().await;
    let dir = tempfile::tempdir()?;
    let fixture = fixture(
        &model,
        HashMap::from([
            ("a".to_string(), server_config(dir.path(), "a")?),
            ("b".to_string(), server_config(dir.path(), "b")?),
        ]),
    )
    .await?;
    let thread = &fixture.codex;
    let rows = thread.mcp_server_selection(None).await?;
    assert_eq!(
        rows.iter()
            .map(|row| (&*row.name, row.selected))
            .collect::<Vec<_>>(),
        vec![("a", false), ("b", false)]
    );
    thread
        .mcp_server_status_snapshot(None, McpSnapshotDetail::ToolsAndAuthOnly)
        .await?;
    let unrelated = completion(&model).await;
    fixture.submit_turn("An unrelated turn").await?;
    assert!(!advertised(&unrelated, "a"));
    assert!(spawns(dir.path(), "a").is_empty());
    assert!(spawns(dir.path(), "b").is_empty());

    thread
        .mcp_server_selection(Some(vec!["a".to_string()]))
        .await?;
    thread.mcp_server_selection(None).await?;
    thread
        .mcp_server_status_snapshot(None, McpSnapshotDetail::Full)
        .await?;
    assert!(
        spawns(dir.path(), "a").is_empty(),
        "applying stages activation until the next turn"
    );
    let first = completion(&model).await;
    fixture.submit_turn("Use selected servers").await?;
    let a = spawns(dir.path(), "a");
    assert_eq!(a.len(), 1);
    assert!(advertised(&first, "a"));
    assert!(
        first.single_request().body_json()["tools"]
            .to_string()
            .contains(&format!("rmcp-test-process-{}", a[0]))
    );

    thread
        .mcp_server_selection(Some(vec!["a".to_string(), "b".to_string()]))
        .await?;
    let both = completion(&model).await;
    fixture.submit_turn("Both servers").await?;
    assert!(advertised(&both, "a") && advertised(&both, "b"));
    assert_eq!(spawns(dir.path(), "a"), a);
    let b = spawns(dir.path(), "b");
    assert_eq!(b.len(), 1);

    thread
        .mcp_server_selection(Some(vec!["b".to_string()]))
        .await?;
    let without_a = completion(&model).await;
    fixture.submit_turn("Only B").await?;
    assert!(!advertised(&without_a, "a") && advertised(&without_a, "b"));
    assert!(
        thread
            .call_mcp_tool("a", "echo", Some(json!({"message":"stale"})), None)
            .await
            .is_err()
    );
    assert!(
        thread
            .read_mcp_resource(
                "a",
                codex_mcp::ReadResourceRequestParams::new("memo://codex/example-note")
            )
            .await
            .is_err()
    );
    assert!(
        alive(&a[0]),
        "deselection must keep the desktop process alive"
    );
    thread
        .mcp_server_status_snapshot(None, McpSnapshotDetail::Full)
        .await?;
    assert_eq!(spawns(dir.path(), "a"), a);

    let second = fixture
        .thread_manager
        .start_thread(StartThreadOptions::new(fixture.config.clone()))
        .await?;
    assert!(
        second
            .thread
            .mcp_server_selection(None)
            .await?
            .iter()
            .all(|row| !row.selected)
    );
    assert!(
        second
            .thread
            .call_mcp_tool("a", "echo", Some(json!({"message":"other thread"})), None)
            .await
            .is_err()
    );
    second.thread.shutdown_and_wait().await?;

    thread
        .mcp_server_selection(Some(vec!["a".to_string(), "b".to_string()]))
        .await?;
    let again = completion(&model).await;
    fixture.submit_turn("Reuse A").await?;
    assert!(advertised(&again, "a"));
    assert_eq!(spawns(dir.path(), "a"), a);
    assert_eq!(spawns(dir.path(), "b"), b);
    thread.shutdown_and_wait().await?;
    stopped(&a[0]).await;
    stopped(&b[0]).await;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_is_fixed_for_all_model_requests_in_a_turn() -> anyhow::Result<()> {
    let model = responses::start_mock_server().await;
    let dir = tempfile::tempdir()?;
    let fixture = fixture(
        &model,
        HashMap::from([("a".to_string(), server_config(dir.path(), "a")?)]),
    )
    .await?;
    fixture
        .codex
        .mcp_server_selection(Some(vec!["a".to_string()]))
        .await?;
    let sequence = responses::mount_sse_sequence(
        &model,
        vec![
            responses::sse(vec![
                responses::ev_response_created("selection-call"),
                responses::ev_function_call_with_namespace(
                    "selection-call",
                    "mcp__a",
                    "sync_readonly",
                    &json!({"sleep_after_ms":250}).to_string(),
                ),
                responses::ev_completed("selection-call"),
            ]),
            responses::sse(vec![responses::ev_completed("selection-done")]),
        ],
    )
    .await;
    fixture
        .codex
        .start_or_steer_turn(TurnInputRequest::user_input(vec![UserInput::Text {
            text: "Call A".to_string(),
            text_elements: Vec::new(),
        }]))
        .await?;
    wait_for_event(&fixture.codex, |event| {
        matches!(event, EventMsg::McpToolCallBegin(_))
    })
    .await;
    assert!(
        fixture
            .codex
            .mcp_server_selection(Some(vec![]))
            .await
            .is_err()
    );
    wait_for_event(&fixture.codex, |event| {
        matches!(event, EventMsg::TurnComplete(_))
    })
    .await;
    let requests = sequence.requests();
    assert_eq!(requests.len(), 2);
    assert!(requests.iter().all(|request| {
        responses::namespace_child_tool(&request.body_json(), "mcp__a", "echo").is_some()
    }));
    fixture.codex.shutdown_and_wait().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_failure_and_cancel_do_not_advertise_unavailable_tools() -> anyhow::Result<()>
{
    let model = responses::start_mock_server().await;
    let dir = tempfile::tempdir()?;
    let mut hanging = server_config(dir.path(), "hanging")?;
    let codex_config::McpServerTransportConfig::Stdio { env, .. } = &mut hanging.transport else {
        unreachable!()
    };
    env.as_mut().unwrap().insert(
        "MCP_TEST_INITIALIZE_BARRIER_FILE".to_string(),
        dir.path()
            .join("never-ready")
            .to_string_lossy()
            .into_owned(),
    );
    let fixture = fixture(&model, HashMap::from([("hanging".to_string(), hanging)])).await?;
    fixture
        .codex
        .mcp_server_selection(Some(vec!["hanging".to_string()]))
        .await?;
    let response = completion(&model).await;
    fixture.submit_turn("Timeout").await?;
    assert!(!advertised(&response, "hanging"));
    assert!(
        fixture
            .codex
            .call_mcp_tool("hanging", "echo", None, None)
            .await
            .is_err()
    );

    fixture
        .codex
        .mcp_server_selection(Some(vec!["hanging".to_string()]))
        .await?;
    fixture
        .codex
        .start_or_steer_turn(TurnInputRequest::user_input(vec![UserInput::Text {
            text: "Cancel startup".to_string(),
            text_elements: Vec::new(),
        }]))
        .await?;
    tokio::time::timeout(Duration::from_secs(5), async {
        while spawns(dir.path(), "hanging").len() < 2 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await?;
    fixture.codex.submit(Op::Interrupt).await?;
    wait_for_event(&fixture.codex, |event| {
        matches!(event, EventMsg::TurnAborted(_))
    })
    .await;
    fixture.codex.shutdown_and_wait().await?;
    for pid in spawns(dir.path(), "hanging") {
        stopped(&pid).await;
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_required_and_policy_blocked_servers_cannot_be_overridden()
-> anyhow::Result<()> {
    let model = responses::start_mock_server().await;
    let dir = tempfile::tempdir()?;
    let mut required = server_config(dir.path(), "required")?;
    required.enabled = true;
    required.required = true;
    let mut blocked = server_config(dir.path(), "blocked")?;
    blocked.enabled = true;
    blocked.required = true;
    blocked.disabled_reason = Some(codex_config::McpServerDisabledReason::Requirements {
        source: codex_config::RequirementSource::Unknown,
    });
    let fixture = fixture(
        &model,
        HashMap::from([
            ("required".to_string(), required),
            ("blocked".to_string(), blocked),
        ]),
    )
    .await?;
    assert!(
        fixture
            .codex
            .mcp_server_selection(Some(vec![]))
            .await
            .is_err()
    );
    assert!(
        fixture
            .codex
            .mcp_server_selection(Some(vec!["required".to_string(), "blocked".to_string()]))
            .await
            .is_err()
    );
    let rows = fixture.codex.mcp_server_selection(None).await?;
    assert!(rows.iter().all(|row| row.locked_reason.is_some()));
    fixture
        .codex
        .mcp_server_selection(Some(vec!["required".to_string()]))
        .await?;
    let response = completion(&model).await;
    fixture.submit_turn("Required only").await?;
    assert!(advertised(&response, "required") && !advertised(&response, "blocked"));
    assert!(spawns(dir.path(), "blocked").is_empty());
    fixture.codex.shutdown_and_wait().await?;
    for pid in spawns(dir.path(), "required") {
        stopped(&pid).await;
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_remote_is_passive_and_dispatch_is_restricted() -> anyhow::Result<()> {
    use wiremock::Mock;
    use wiremock::ResponseTemplate;
    use wiremock::matchers::method;
    use wiremock::matchers::path;
    let model = responses::start_mock_server().await;
    let remote = MockServer::start().await;
    Mock::given(method("POST")).and(path("/mcp"))
        .respond_with(|request: &wiremock::Request| {
            let request: serde_json::Value = serde_json::from_slice(&request.body).unwrap();
            let result = match request["method"].as_str() {
                Some("initialize") => json!({"protocolVersion":"2025-11-25", "capabilities":{"tools":{}}, "serverInfo":{"name":"remote", "version":"1.0"}}),
                Some("tools/list") => json!({"tools":[{"name":"echo", "inputSchema":{"type":"object"}}]}),
                Some("resources/list") => json!({"resources":[]}),
                Some("resources/templates/list") => json!({"resourceTemplates":[]}),
                Some("tools/call") => json!({"content":[{"type":"text", "text":"remote result"}]}),
                _ => return ResponseTemplate::new(202),
            };
            ResponseTemplate::new(200).set_body_json(json!({"jsonrpc":"2.0", "id":request["id"], "result":result}))
        }).mount(&remote).await;
    let config =
        serde_json::from_value(json!({"url":format!("{}/mcp", remote.uri()), "enabled":false}))?;
    let fixture = fixture(&model, HashMap::from([("remote".to_string(), config)])).await?;
    fixture.codex.mcp_server_selection(None).await?;
    fixture
        .codex
        .mcp_server_status_snapshot(None, McpSnapshotDetail::Full)
        .await?;
    assert!(remote.received_requests().await.unwrap().is_empty());
    fixture
        .codex
        .mcp_server_selection(Some(vec!["remote".to_string()]))
        .await?;
    let selected = completion(&model).await;
    fixture.submit_turn("Activate remote").await?;
    assert!(advertised(&selected, "remote"));
    fixture
        .codex
        .call_mcp_tool("remote", "echo", None, None)
        .await?;
    fixture.codex.mcp_server_selection(Some(vec![])).await?;
    let disabled = completion(&model).await;
    fixture.submit_turn("Deactivate remote").await?;
    assert!(!advertised(&disabled, "remote"));
    assert!(
        fixture
            .codex
            .call_mcp_tool("remote", "echo", None, None)
            .await
            .is_err()
    );
    fixture
        .codex
        .mcp_server_selection(Some(vec!["remote".to_string()]))
        .await?;
    completion(&model).await;
    fixture.submit_turn("Reuse remote").await?;
    let requests = remote.received_requests().await.unwrap();
    assert_eq!(
        requests
            .iter()
            .filter(|r| serde_json::from_slice::<serde_json::Value>(&r.body)
                .is_ok_and(|v| v["method"] == "initialize"))
            .count(),
        1
    );
    fixture.codex.shutdown_and_wait().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_crash_retry_and_concurrent_discovery_start_once() -> anyhow::Result<()> {
    let model = responses::start_mock_server().await;
    let dir = tempfile::tempdir()?;
    let mut config = server_config(dir.path(), "a")?;
    let exit = dir.path().join("exit");
    let codex_config::McpServerTransportConfig::Stdio { env, .. } = &mut config.transport else {
        unreachable!()
    };
    env.as_mut().unwrap().insert(
        "MCP_TEST_EXIT_FILE".to_string(),
        exit.to_string_lossy().into_owned(),
    );
    let fixture = fixture(&model, HashMap::from([("a".to_string(), config)])).await?;
    fixture
        .codex
        .mcp_server_selection(Some(vec!["a".to_string()]))
        .await?;
    completion(&model).await;
    fixture.submit_turn("Start A").await?;
    let first = spawns(dir.path(), "a");
    assert_eq!(first.len(), 1);
    fs::write(&exit, "exit")?;
    stopped(&first[0]).await;
    assert!(
        fixture
            .codex
            .call_mcp_tool("a", "echo", None, None)
            .await
            .is_err()
    );
    fs::remove_file(&exit)?;
    fixture
        .codex
        .mcp_server_selection(Some(vec!["a".to_string()]))
        .await?;
    let selected = completion(&model).await;
    fixture.submit_turn("Retry A").await?;
    assert!(advertised(&selected, "a"));
    let pids = spawns(dir.path(), "a");
    assert_eq!(pids.len(), 2);
    let (left, right) = tokio::join!(
        fixture
            .codex
            .call_mcp_tool("a", "echo", Some(json!({"message":"left"})), None),
        fixture
            .codex
            .call_mcp_tool("a", "echo", Some(json!({"message":"right"})), None),
    );
    assert!(left.is_ok() && right.is_ok());
    assert_eq!(spawns(dir.path(), "a"), pids);
    fixture.codex.shutdown_and_wait().await?;
    stopped(&pids[1]).await;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_subagent_cannot_expand_parent_availability() -> anyhow::Result<()> {
    let model = responses::start_mock_server().await;
    let dir = tempfile::tempdir()?;
    let fixture = fixture(
        &model,
        HashMap::from([("a".to_string(), server_config(dir.path(), "a")?)]),
    )
    .await?;
    let mut config = fixture.config.clone();
    config.mcp_server_selection = Some(Default::default());
    let mut options = StartThreadOptions::new(config);
    options.session_source = Some(codex_protocol::protocol::SessionSource::SubAgent(
        codex_protocol::protocol::SubAgentSource::Other("selection-test".to_string()),
    ));
    let child = fixture.thread_manager.start_thread(options).await?;
    assert!(
        child
            .thread
            .mcp_server_selection(Some(vec!["a".to_string()]))
            .await
            .is_err()
    );
    assert!(
        child
            .thread
            .mcp_server_selection(None)
            .await?
            .iter()
            .all(|row| !row.selected && row.locked_reason.is_some())
    );
    assert!(
        child
            .thread
            .call_mcp_tool("a", "echo", None, None)
            .await
            .is_err()
    );
    assert!(spawns(dir.path(), "a").is_empty());
    child.thread.shutdown_and_wait().await?;
    fixture.codex.shutdown_and_wait().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_concurrent_initialization_has_one_process() -> anyhow::Result<()> {
    let model = responses::start_mock_server().await;
    let dir = tempfile::tempdir()?;
    let barrier = dir.path().join("initialize-ready");
    let mut config = server_config(dir.path(), "a")?;
    let codex_config::McpServerTransportConfig::Stdio { env, .. } = &mut config.transport else {
        unreachable!()
    };
    env.as_mut().unwrap().insert(
        "MCP_TEST_INITIALIZE_BARRIER_FILE".to_string(),
        barrier.to_string_lossy().into_owned(),
    );
    let fixture = fixture(&model, HashMap::from([("a".to_string(), config)])).await?;
    fixture
        .codex
        .mcp_server_selection(Some(vec!["a".to_string()]))
        .await?;
    let response = completion(&model).await;
    fixture
        .codex
        .start_or_steer_turn(TurnInputRequest::user_input(vec![UserInput::Text {
            text: "Activate A".to_string(),
            text_elements: Vec::new(),
        }]))
        .await?;
    tokio::time::timeout(Duration::from_secs(5), async {
        while spawns(dir.path(), "a").is_empty() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await?;
    let left = std::sync::Arc::clone(&fixture.codex);
    let right = std::sync::Arc::clone(&fixture.codex);
    let left = tokio::spawn(async move {
        left.call_mcp_tool("a", "echo", Some(json!({"message":"left"})), None)
            .await
    });
    let right = tokio::spawn(async move {
        right
            .call_mcp_tool("a", "echo", Some(json!({"message":"right"})), None)
            .await
    });
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert!(
        response.requests().is_empty(),
        "model must wait for explicit discovery"
    );
    assert_eq!(spawns(dir.path(), "a").len(), 1);
    fs::write(barrier, "ready")?;
    assert!(left.await?.is_ok() && right.await?.is_ok());
    wait_for_event(&fixture.codex, |event| {
        matches!(event, EventMsg::TurnComplete(_))
    })
    .await;
    assert!(advertised(&response, "a"));
    assert_eq!(spawns(dir.path(), "a").len(), 1);
    fixture.codex.shutdown_and_wait().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_config_change_replaces_only_affected_connection() -> anyhow::Result<()> {
    let model = responses::start_mock_server().await;
    let dir = tempfile::tempdir()?;
    let fixture = fixture(
        &model,
        HashMap::from([
            ("a".to_string(), server_config(dir.path(), "a")?),
            ("b".to_string(), server_config(dir.path(), "b")?),
        ]),
    )
    .await?;
    fixture
        .codex
        .mcp_server_selection(Some(vec!["a".to_string(), "b".to_string()]))
        .await?;
    completion(&model).await;
    fixture.submit_turn("Both").await?;
    let a = spawns(dir.path(), "a");
    let b = spawns(dir.path(), "b");
    let mut next = fixture.config.clone();
    let mut servers = next.mcp_servers.get().clone();
    let codex_config::McpServerTransportConfig::Stdio { env, .. } =
        &mut servers.get_mut("a").unwrap().transport
    else {
        unreachable!()
    };
    env.as_mut()
        .unwrap()
        .insert("MCP_TEST_CONFIG_CHANGE".to_string(), "1".to_string());
    next.mcp_servers.set(servers)?;
    fixture.codex.refresh_mcp_config(next).await;
    completion(&model).await;
    fixture.submit_turn("Updated A").await?;
    assert_eq!(spawns(dir.path(), "a").len(), 2);
    assert_eq!(spawns(dir.path(), "b"), b);
    stopped(&a[0]).await;
    fixture.codex.shutdown_and_wait().await?;
    for pid in spawns(dir.path(), "a").iter().chain(b.iter()) {
        stopped(pid).await;
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_selection_stale_model_call_cannot_bypass_deselection() -> anyhow::Result<()> {
    let model = responses::start_mock_server().await;
    let dir = tempfile::tempdir()?;
    let fixture = fixture(
        &model,
        HashMap::from([("a".to_string(), server_config(dir.path(), "a")?)]),
    )
    .await?;
    fixture
        .codex
        .mcp_server_selection(Some(vec!["a".to_string()]))
        .await?;
    let initial = responses::mount_sse_sequence(
        &model,
        vec![
            responses::sse(vec![
                responses::ev_function_call_with_namespace(
                    "original-call",
                    "mcp__a",
                    "echo",
                    &json!({"message":"original"}).to_string(),
                ),
                responses::ev_completed("original"),
            ]),
            responses::sse(vec![responses::ev_completed("original-done")]),
        ],
    )
    .await;
    fixture.submit_turn("Use A").await?;
    assert!(
        initial
            .function_call_output_text("original-call")
            .unwrap()
            .contains(&format!("rmcp-test-process-{}", spawns(dir.path(), "a")[0]))
    );
    fixture.codex.mcp_server_selection(Some(vec![])).await?;
    let stale = responses::mount_sse_sequence(
        &model,
        vec![
            responses::sse(vec![
                responses::ev_function_call_with_namespace(
                    "stale-call",
                    "mcp__a",
                    "echo",
                    &json!({"message":"must not execute"}).to_string(),
                ),
                responses::ev_completed("stale"),
            ]),
            responses::sse(vec![responses::ev_completed("stale-done")]),
        ],
    )
    .await;
    fixture.submit_turn("A is now excluded").await?;
    assert!(
        stale
            .requests()
            .iter()
            .all(|r| responses::namespace_child_tool(&r.body_json(), "mcp__a", "echo").is_none())
    );
    let output = stale.function_call_output_text("stale-call").unwrap();
    assert!(
        !output.contains("rmcp-test-process-"),
        "deselected tool executed: {output}"
    );
    assert!(
        output.contains("not found")
            || output.contains("Unknown")
            || output.contains("unknown")
            || output.contains("unavailable")
            || output.contains("unsupported call"),
        "expected dispatch rejection: {output}"
    );
    assert_eq!(spawns(dir.path(), "a").len(), 1);
    assert!(alive(&spawns(dir.path(), "a")[0]));
    fixture.codex.shutdown_and_wait().await?;
    Ok(())
}
