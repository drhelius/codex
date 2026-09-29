//! Conversation-only availability. Registration and connection ownership are independent.

use std::collections::HashSet;

use codex_protocol::mcp::McpServerConnectionStatus;

use super::McpRuntime;
use crate::McpServerSource;

#[derive(Default)]
pub(super) struct ConversationSelection {
    pub(super) active: Option<HashSet<String>>,
    pending: Option<HashSet<String>>,
}

/// Passive selector row; a connected server may be unselected and retained for reuse.
pub struct McpServerSelectionEntry {
    pub name: String,
    pub selected: bool,
    pub locked_reason: Option<String>,
    pub connection_status: McpServerConnectionStatus,
}

impl McpRuntime {
    /// The immutable availability choice used by the running turn.
    pub fn active_selection(&self) -> Option<HashSet<String>> {
        self.selection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .active
            .clone()
    }

    /// Choice inherited by the next turn and its children, never serialized to a rollout.
    pub fn next_selection(&self) -> Option<HashSet<String>> {
        let selection = self
            .selection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        selection
            .pending
            .as_ref()
            .or(selection.active.as_ref())
            .cloned()
    }

    /// Validate the complete choice before staging it. This never creates a connection.
    pub fn stage_selection(&self, selected: HashSet<String>) -> anyhow::Result<()> {
        let current = self.current.load();
        let config = current
            .config
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("MCP is not ready"))?;
        let servers = config.mcp_server_catalog.configured_servers();
        for name in &selected {
            let registration = config
                .mcp_server_catalog
                .server(name)
                .filter(|server| matches!(server.source(), McpServerSource::Config))
                .ok_or_else(|| anyhow::anyhow!("MCP server '{name}' is not user-configured"))?;
            if let Some(reason) = &registration.config().disabled_reason {
                anyhow::bail!("MCP server '{name}' is unavailable: {reason}");
            }
        }
        for (name, server) in &servers {
            if config
                .mcp_server_catalog
                .server(name)
                .is_some_and(|registration| {
                    matches!(registration.source(), McpServerSource::Config)
                })
                && server.required
                && selected.contains(name) != (server.enabled && server.disabled_reason.is_none())
            {
                anyhow::bail!(
                    "Required MCP server '{name}' must follow its configured availability"
                );
            }
        }
        self.selection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .pending = Some(selected);
        Ok(())
    }

    /// Called only when preparing a new complete turn, before any model request.
    pub fn commit_selection(&self) -> bool {
        let mut selection = self
            .selection
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Some(pending) = selection.pending.take() {
            selection.active = Some(pending);
            true
        } else {
            false
        }
    }

    pub async fn selection_entries(&self) -> Vec<McpServerSelectionEntry> {
        let current = self.current.load_full();
        let Some(config) = &current.config else {
            return Vec::new();
        };
        let selected = self.next_selection();
        let statuses = current.connections.retained_connection_statuses().await;
        let mut entries = config
            .mcp_server_catalog
            .configured_servers()
            .into_iter()
            .filter(|(name, _)| {
                config
                    .mcp_server_catalog
                    .server(name)
                    .is_some_and(|server| matches!(server.source(), McpServerSource::Config))
            })
            .map(|(name, server)| {
                let locked_reason = server
                    .disabled_reason
                    .as_ref()
                    .map(ToString::to_string)
                    .or_else(|| {
                        server
                            .required
                            .then(|| "required; follows configuration".to_string())
                    });
                let enabled = if server.disabled_reason.is_some() {
                    false
                } else if server.required {
                    server.enabled
                } else {
                    selected
                        .as_ref()
                        .map_or(server.enabled, |names| names.contains(&name))
                };
                McpServerSelectionEntry {
                    connection_status: statuses
                        .get(&name)
                        .copied()
                        .unwrap_or(McpServerConnectionStatus::NotStarted),
                    selected: enabled,
                    name,
                    locked_reason,
                }
            })
            .collect::<Vec<_>>();
        entries.sort_by(|left, right| left.name.cmp(&right.name));
        entries
    }
}
