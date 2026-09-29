//! Conversation-local MCP server selection over the app-server transport.

use super::*;
use codex_app_server_protocol::McpServerSelectionParams;
use codex_app_server_protocol::McpServerSelectionResponse;
use codex_app_server_protocol::RequestId;

impl App {
    pub(super) async fn mcp_selection(
        &mut self,
        app_server: &AppServerSession,
        thread_id: ThreadId,
        servers: Option<Vec<String>>,
    ) {
        if self.current_displayed_thread_id() != Some(thread_id) {
            return;
        }
        if self.chat_widget.is_agent_turn_running() {
            self.chat_widget.add_error_message(
                "MCP selection can only change between complete turns.".to_string(),
            );
            return;
        }
        let apply = servers.is_some();
        let result = app_server
            .request_handle()
            .request_typed::<McpServerSelectionResponse>(ClientRequest::McpServerSelection {
                request_id: RequestId::String(format!("mcp-selection-{}", Uuid::new_v4())),
                params: McpServerSelectionParams {
                    thread_id: thread_id.to_string(),
                    selected_servers: servers,
                },
            })
            .await;
        match result {
            Ok(_) if apply => self.chat_widget.add_info_message(
                "MCP selection saved for the next turn in this conversation.".to_string(),
                /*hint*/ None,
            ),
            Ok(response) => {
                self.chat_widget
                    .show_bottom_pane_view(crate::bottom_pane::mcp_selection_picker(
                        thread_id,
                        response.servers,
                        self.app_event_tx.clone(),
                    ))
            }
            Err(error) => self
                .chat_widget
                .add_error_message(format!("MCP selection: {error}")),
        }
    }
}
