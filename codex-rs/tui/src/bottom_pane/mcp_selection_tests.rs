use super::*;
use codex_app_server_protocol::McpServerConnectionStatus;
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use crossterm::event::KeyModifiers;
use pretty_assertions::assert_eq;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use tokio::sync::mpsc::unbounded_channel;

fn entries() -> Vec<McpServerSelectionEntry> {
    vec![
        McpServerSelectionEntry {
            name: "desktop".to_string(),
            selected: false,
            locked_reason: None,
            connection_status: McpServerConnectionStatus::NotStarted,
        },
        McpServerSelectionEntry {
            name: "required".to_string(),
            selected: true,
            locked_reason: Some("required; follows configuration".to_string()),
            connection_status: McpServerConnectionStatus::Connected,
        },
        McpServerSelectionEntry {
            name: "restricted".to_string(),
            selected: false,
            locked_reason: Some("blocked by administrator".to_string()),
            connection_status: McpServerConnectionStatus::Disabled,
        },
    ]
}

#[test]
fn mcp_selector_cancel_has_no_side_effects() {
    let (tx, mut rx) = unbounded_channel();
    let mut picker = mcp_selection_picker(ThreadId::new(), entries(), AppEventSender::new(tx));
    picker.handle_key_event(KeyEvent::new(KeyCode::Char(' '), KeyModifiers::NONE));
    picker.handle_key_event(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    picker.handle_key_event(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
    assert!(picker.is_complete());
    assert!(rx.try_recv().is_err());
}

#[test]
fn mcp_selector_applies_once_and_preserves_locked_rows() {
    let (tx, mut rx) = unbounded_channel();
    let thread_id = ThreadId::new();
    let mut picker = mcp_selection_picker(thread_id, entries(), AppEventSender::new(tx));
    picker.handle_key_event(KeyEvent::new(KeyCode::Char(' '), KeyModifiers::NONE));
    for _ in 0..2 {
        picker.handle_key_event(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        picker.handle_key_event(KeyEvent::new(KeyCode::Char(' '), KeyModifiers::NONE));
    }
    picker.handle_key_event(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let AppEvent::SetMcpSelection {
        thread_id: actual,
        servers,
    } = rx.try_recv().unwrap()
    else {
        panic!("expected conversation selection");
    };
    assert_eq!(
        (actual, servers),
        (
            thread_id,
            vec!["desktop".to_string(), "required".to_string()]
        )
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn mcp_selector_appearance() {
    let (tx, _) = unbounded_channel();
    let picker = mcp_selection_picker(ThreadId::new(), entries(), AppEventSender::new(tx));
    let area = Rect::new(0, 0, 90, picker.desired_height(90));
    let mut buffer = Buffer::empty(area);
    picker.render(area, &mut buffer);
    insta::assert_snapshot!(format!("{buffer:?}"));
}
