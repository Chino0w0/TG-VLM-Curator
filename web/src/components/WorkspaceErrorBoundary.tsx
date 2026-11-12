import { Component, type ReactNode } from "react";

/** Keep authentication and navigation usable if one workspace fails to render. */
export class WorkspaceErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <section role="alert" className="panel">
      <h1>Workspace could not be displayed</h1>
      <p>The server response could not be rendered. Other workspaces remain available. No action has been automatically retried.</p>
      <button className="btn" onClick={() => this.setState({ failed: false })}>Retry workspace</button>
    </section> : this.props.children;
  }
}
