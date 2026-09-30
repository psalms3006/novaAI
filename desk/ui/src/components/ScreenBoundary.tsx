import React from 'react';

/**
 * One screen failing must not take the window with it. The Skills screen once
 * threw on an unexpected response and, with no boundary anywhere, the whole
 * window went blank. This shows what broke and lets the person carry on.
 */
export class ScreenBoundary extends React.Component<
  { name: string; children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    // Visible in the webview console and in Diagnostics' event stream.
    console.error(`[NOVA] ${this.props.name} failed:`, error);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" className="max-w-md mx-auto p-6 rounded-2xl border text-center space-y-3"
        style={{ borderColor: 'rgba(239,68,68,0.35)', backgroundColor: 'rgba(239,68,68,0.08)', color: '#ef4444' }}>
        <div className="text-sm font-medium">This part of NOVA hit a problem.</div>
        <div className="text-[11px] opacity-80 break-words">{this.state.error.message}</div>
        <button type="button" onClick={() => this.setState({ error: null })}
          className="px-3.5 py-1.5 rounded-xl border text-xs font-medium cursor-pointer"
          style={{ borderColor: 'rgba(239,68,68,0.35)' }}>
          Try again
        </button>
      </div>
    );
  }
}
