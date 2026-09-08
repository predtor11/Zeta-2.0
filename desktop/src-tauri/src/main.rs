// Zeta Desktop: a Tauri window that hosts the local Zeta UI.
// The Python backend must be running (start.bat --no-browser). Bundling it as a sidecar is a TODO.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running Zeta desktop");
}
