//! Server selection is transient and is committed by run_turn, never by a status read.

use super::*;

impl Session {
    pub(crate) async fn mcp_server_selection(
        &self,
        selected: Option<Vec<String>>,
    ) -> anyhow::Result<Vec<codex_mcp::McpServerSelectionEntry>> {
        let is_subagent = matches!(
            self.state.lock().await.session_configuration.session_source,
            SessionSource::SubAgent(_)
        );
        if let Some(selected) = selected {
            anyhow::ensure!(
                !is_subagent,
                "Subagents inherit their parent turn's MCP selection"
            );
            // A terminal event can arrive just before its empty turn reservation is cleared.
            // Allow that bounded cleanup to finish, but reject a running task immediately.
            let stage = async {
                loop {
                    {
                        let active = self.active_turn.lock().await;
                        if active.is_none() {
                            return self
                                .services
                                .mcp_runtime
                                .stage_selection(selected.into_iter().collect());
                        }
                        anyhow::ensure!(
                            active.as_ref().is_some_and(|turn| turn.task.is_none()),
                            "MCP selection can only change between complete turns"
                        );
                    }
                    tokio::time::sleep(std::time::Duration::from_millis(1)).await;
                }
            };
            tokio::time::timeout(std::time::Duration::from_millis(100), stage)
                .await
                .map_err(|_| {
                    anyhow::anyhow!("MCP selection can only change between complete turns")
                })??;
        }
        let mut entries = self.services.mcp_runtime.selection_entries().await;
        if is_subagent {
            for entry in &mut entries {
                entry.locked_reason = Some("inherited from parent turn".to_string());
            }
        }
        Ok(entries)
    }
}
