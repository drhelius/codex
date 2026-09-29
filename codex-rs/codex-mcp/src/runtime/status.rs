//! Read selected-server metadata from one published thread runtime.

use std::sync::Arc;

use super::McpRuntime;
use super::McpRuntimeContext;
use crate::McpConfig;
use crate::McpServerStatusSnapshot;
use crate::McpSnapshotDetail;
use crate::mcp::collect_mcp_server_status_snapshot_from_manager;
use crate::mcp::compute_auth_statuses;
use crate::mcp::effective_mcp_servers;

impl McpRuntime {
    /// Reuses the selected connection and catalog without starting unrelated servers.
    /// Configuration and metadata belong to the same captured runtime publication.
    pub async fn server_status_snapshot(
        &self,
        server: Option<&str>,
        detail: McpSnapshotDetail,
        runtime_context: &McpRuntimeContext,
    ) -> anyhow::Result<(Arc<McpConfig>, McpServerStatusSnapshot)> {
        let current = self.current.load_full();
        let runtime_context = runtime_context.clone().with_selected_environments(
            Arc::clone(&current.environment_selections),
            current.ready_environments.clone(),
        );
        let config = Arc::clone(
            current
                .config
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("MCP runtime is not configured"))?,
        );
        let mut servers = effective_mcp_servers(&config, current.auth.as_ref());
        servers.retain(|name, _| server.is_none_or(|server| name == server));
        let auth_statuses = compute_auth_statuses(
            servers.iter(),
            config.mcp_oauth_credentials_store_mode,
            config.auth_keyring_backend_kind,
            current.auth.as_ref(),
            &runtime_context,
        )
        .await;
        let server_names = servers.keys().cloned().collect::<Vec<_>>();
        let statuses = current.connections.connection_statuses().await;
        let ready_names = server_names
            .iter()
            .filter(|name| {
                statuses.get(*name)
                    == Some(&codex_protocol::mcp::McpServerConnectionStatus::Connected)
            })
            .cloned()
            .collect();
        let mut snapshot = collect_mcp_server_status_snapshot_from_manager(
            &current.connections,
            auth_statuses,
            ready_names,
            detail,
        )
        .await;
        snapshot.server_names = server_names;
        Ok((config, snapshot))
    }
}
