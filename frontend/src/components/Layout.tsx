import React from "react";

export type Page =
  | "dashboard"
  | "metrics"
  | "use-cases"
  | "auth-providers"
  | "metric-prompt";

interface LayoutProps {
  currentPage: Page;
  onNavigate: (page: Page) => void;
  children: React.ReactNode;
}

const NAV_ITEMS: { page: Page; label: string; icon: string }[] = [
  { page: "dashboard", label: "Dashboard", icon: "📊" },
  { page: "metrics", label: "Metrics", icon: "📏" },
  { page: "use-cases", label: "Use Cases", icon: "🧩" },
  { page: "auth-providers", label: "Auth Providers", icon: "🔐" },
];

/** Header titles, including the pages the sidebar does not list. */
const PAGE_TITLES: Record<Page, string> = {
  dashboard: "Dashboard",
  metrics: "Metrics",
  "use-cases": "Use Cases",
  "auth-providers": "Auth Providers",
  "metric-prompt": "Metric Prompt",
};

/**
 * App shell with a lateral sidebar menu and a top header bar.
 */
export function Layout({ currentPage, onNavigate, children }: LayoutProps) {
  const activePage = currentPage === "metric-prompt" ? "metrics" : currentPage;

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <span className="sidebar-logo">⚡</span>
          <span className="sidebar-title">Scorekeeper</span>
        </div>
        <nav className="sidebar-nav">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.page}
              className={`sidebar-nav-item ${activePage === item.page ? "sidebar-nav-active" : ""}`}
              onClick={() => onNavigate(item.page)}
              aria-current={activePage === item.page ? "page" : undefined}
            >
              <span className="sidebar-nav-icon">{item.icon}</span>
              <span className="sidebar-nav-label">{item.label}</span>
            </button>
          ))}
        </nav>
      </aside>

      <div className="app-main">
        <header className="app-header">
          <h2 className="app-header-title">
            {PAGE_TITLES[currentPage]}
          </h2>
          <div className="app-header-search">
            <span className="search-icon">🔍</span>
            <input
              type="text"
              className="search-input"
              placeholder="Search…"
              aria-label="Search"
            />
          </div>
        </header>
        <div className="app-content">
          {children}
        </div>
      </div>
    </div>
  );
}
