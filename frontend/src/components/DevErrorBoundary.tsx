import React from "react";

type State = { error: Error | null; info: React.ErrorInfo | null };

export class DevErrorBoundary extends React.Component<
  { children: React.ReactNode },
  State
> {
  state: State = { error: null, info: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    this.setState({ error, info });
    console.error("DevErrorBoundary caught:", error, info);
  }

  reset = () => this.setState({ error: null, info: null });

  render() {
    if (!this.state.error) return this.props.children;
    const { error, info } = this.state;
    return (
      <div
        style={{
          margin: 16,
          padding: 16,
          border: "2px solid #c62828",
          background: "#fff5f5",
          color: "#222",
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          fontSize: 13,
          whiteSpace: "pre-wrap",
          borderRadius: 6,
        }}
      >
        <div
          style={{
            fontWeight: 700,
            color: "#c62828",
            marginBottom: 8,
            fontSize: 14,
          }}
        >
          {error.name}: {error.message}
        </div>
        <details open style={{ marginBottom: 8 }}>
          <summary style={{ cursor: "pointer" }}>Stack</summary>
          <div>{error.stack}</div>
        </details>
        {info?.componentStack && (
          <details>
            <summary style={{ cursor: "pointer" }}>Component stack</summary>
            <div>{info.componentStack}</div>
          </details>
        )}
        <button
          onClick={this.reset}
          style={{
            marginTop: 12,
            padding: "4px 10px",
            border: "1px solid #888",
            background: "#fff",
            cursor: "pointer",
            borderRadius: 4,
          }}
        >
          Try again
        </button>
      </div>
    );
  }
}
