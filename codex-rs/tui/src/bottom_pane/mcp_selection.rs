//! The picker edits a local draft. Only Enter emits an app-server mutation.

use codex_app_server_protocol::McpServerSelectionEntry;
use codex_protocol::ThreadId;

use super::BottomPaneView;
use super::multi_select_picker::MultiSelectItem;
use super::multi_select_picker::MultiSelectPicker;
use crate::app_event::AppEvent;
use crate::app_event_sender::AppEventSender;

pub(crate) fn mcp_selection_picker(
    thread_id: ThreadId,
    servers: Vec<McpServerSelectionEntry>,
    tx: AppEventSender,
) -> Box<dyn BottomPaneView> {
    let locked = servers
        .iter()
        .filter(|server| server.locked_reason.is_some())
        .map(|server| server.name.clone())
        .collect();
    let items = servers
        .into_iter()
        .map(|server| MultiSelectItem {
            id: server.name.clone(),
            name: server.name,
            description: Some(
                server
                    .locked_reason
                    .unwrap_or_else(|| format!("{:?}", server.connection_status)),
            ),
            enabled: server.selected,
            ..Default::default()
        })
        .collect();
    Box::new(MultiSelectPicker::builder(
        "MCP servers".to_string(),
        Some("Applies to the next complete turn. Connections are kept until this conversation ends.".to_string()),
        tx,
    ).items(items).locked_ids(locked).on_confirm(move |names, tx| {
        tx.send(AppEvent::SetMcpSelection { thread_id, servers: names.to_vec() });
    }).build())
}

#[cfg(test)]
#[path = "mcp_selection_tests.rs"]
mod tests;
