import * as vscode from "vscode";
import { randomBytes } from "node:crypto";
import { ServerManager } from "./server-manager";

/**
 * Webview HTML that embeds the running Python dashboard via an iframe.
 *
 * Two states:
 *   - When `url` is set we render the iframe at that URL.
 *   - When `url` is null we render a status / error pane (with the latest
 *     status text the host pushed via setStatus).
 *
 * We rely on VS Code's webview Content-Security-Policy. Allowing the
 * dashboard's localhost origin via `frame-src http://127.0.0.1:* http://localhost:*`
 * is enough; the dashboard ships its own CSP for what it loads inside.
 *
 * The iframe sandbox includes `allow-downloads` so the dashboard's CSV export
 * (a Blob + `a.download` click) works inside the webview — without it Chromium
 * silently blocks the download.
 */
export function renderHtml(
  url: string | null,
  statusText: string,
  nonce: string,
  iconUri = "",
  cspSource = "",
  showRetry = false,
): string {
  if (url) {
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; frame-src http://127.0.0.1:* http://localhost:*; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Claude Usage</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; background: #161617; }
  iframe { border: 0; width: 100%; height: 100vh; display: block; }
</style>
</head>
<body>
<iframe src="${escapeHtml(url)}" sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"></iframe>
</body>
</html>`;
  }

  // Status / loading pane — styled to match the dashboard header (same icon,
  // title, and elevated-palette colors) so the cold-start screen doesn't jar.
  const imgSrc = cspSource ? ` img-src ${cspSource};` : "";
  const logo = iconUri ? `<span class="logo"></span>` : "";
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none';${imgSrc} style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<title>Claude Usage</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #BFBFBF; background: #161617; padding: 24px; line-height: 1.5; }
  .brand { display: flex; align-items: center; gap: 10px; margin: 0 0 18px; }
  .brand .logo { width: 26px; height: 26px; flex-shrink: 0; background-color: #BFBFBF; -webkit-mask: url("${iconUri}") no-repeat center / contain; mask: url("${iconUri}") no-repeat center / contain; }
  .brand h1 { font-size: 18px; font-weight: 600; color: #BFBFBF; margin: 0; }
  p { color: #BFBFBF; font-size: 13px; margin: 0 0 8px; }
  p.hint { color: #6F6F70; }
  code { background: #1E1F20; border: 1px solid #2C2D2E; border-radius: 4px; padding: 1px 5px; font-size: 12px; }
  a.retry { display: inline-block; margin: 6px 0 14px; padding: 6px 14px; font-size: 13px; font-weight: 600; color: #161617; background: #BFBFBF; border-radius: 6px; text-decoration: none; }
  a.retry:hover { background: #D8D8D8; }
</style>
</head>
<body>
<div class="brand">${logo}<h1>Claude Code Usage</h1></div>
<p>${escapeHtml(statusText) || "The dashboard server is not running yet."}</p>
${showRetry ? `<p><a class="retry" href="command:claudeUsage.open">&#8635; Retry</a></p>` : ""}
<p class="hint">Run <code>Claude Usage: Open Dashboard</code> from the command palette.</p>
</body>
</html>`;
}

/**
 * Escape HTML for safe interpolation into the templates above.
 * Exported for testability.
 */
export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Generate a one-shot nonce for the CSP script-src directive.
 * 24 bytes of crypto-random output, base64url-encoded (URL-safe, no
 * padding, always 32 chars).
 */
export function makeNonce(): string {
  return randomBytes(24).toString("base64url");
}

export class DashboardSidebar implements vscode.WebviewViewProvider {
  public static readonly viewId = "claudeUsage.dashboard";

  private view: vscode.WebviewView | undefined;
  private currentUrl: string | null = null;
  private statusText = "";
  // Only true after a start attempt actually failed — gates the Retry button so
  // it doesn't show during the normal "starting…" / not-yet-run states.
  private failed = false;
  private readonly onShow: () => void;
  private readonly extensionUri: vscode.Uri | undefined;
  private iconUri = "";
  private cspSource = "";

  constructor(onShow: () => void = () => {}, extensionUri?: vscode.Uri) {
    this.onShow = onShow;
    this.extensionUri = extensionUri;
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    // enableCommandUris lets the status pane's "Retry" link invoke
    // claudeUsage.open via a command: URI. The pane renders only our own
    // trusted HTML (statusText is escaped), so allowing command URIs is safe.
    view.webview.options = { enableScripts: true, enableCommandUris: true };
    // Resolve a webview-safe URI for the bundled icon so the status pane shows
    // the same logo as the dashboard header. Guarded so node-only tests (whose
    // fake view has no asWebviewUri / no vscode.Uri) don't blow up.
    if (this.extensionUri && typeof view.webview.asWebviewUri === "function") {
      this.iconUri = view.webview
        .asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "resources", "icon.svg"))
        .toString();
      this.cspSource = view.webview.cspSource ?? "";
    }
    this.render();
    view.onDidDispose(() => {
      this.view = undefined;
    });
    // Kick the host to start the server now that the user has revealed the
    // panel. extension.ts wires this to openDashboard(); the in-flight
    // coalescing on that side means clicking the icon repeatedly is safe.
    this.onShow();
  }

  /** Called from extension.ts after the server is ready. */
  setUrl(url: string | null): void {
    this.currentUrl = url;
    this.render();
  }

  /** Non-error status (initial / "starting…"): no Retry button. */
  setStatus(text: string): void {
    this.statusText = text;
    this.failed = false;
    this.render();
  }

  /** A start attempt failed: show the status plus a Retry button. */
  setError(text: string): void {
    this.statusText = text;
    this.failed = true;
    this.render();
  }

  /** Force the iframe to reload (e.g. after a rescan). */
  refresh(): void {
    if (!this.view) return;
    // Re-render same URL — the iframe will reload because the HTML is regenerated.
    this.render();
  }

  private render(): void {
    if (!this.view) return;
    this.view.webview.html = renderHtml(this.currentUrl, this.statusText, makeNonce(), this.iconUri, this.cspSource, this.failed);
  }
}

// Note: in extension.ts we connect ServerManager to DashboardSidebar via:
//   server.start().then(() => sidebar.setUrl(`http://${host}:${port}/`))
// Importing ServerManager here only so the type stays in the module graph; not
// used at runtime. Stripped on build.
export type { ServerManager };
