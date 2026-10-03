import ReactDOM from "react-dom/client";
import ApiDesign from "./ApiDesign";
import App from "./App";
import Requirements from "./Requirements";
import Workflows from "./Workflows";
import "./styles.css";
import React, { useState } from "react";

type Mode = "chat" | "requirements" | "api-design" | "workflows";

const modes: [Mode, string][] = [
  ["chat", "Chat"],
  ["requirements", "Requirements"],
  ["api-design", "API design"],
  ["workflows", "Workflows"],
];

function modeFromUrl(): Mode {
  const mode = new URLSearchParams(window.location.search).get("mode");
  return modes.find(([item]) => item === mode)?.[0] ?? "chat";
}

function Shell() {
  const [mode, setMode] = useState<Mode>(modeFromUrl);

  function switchMode(next: Mode) {
    const url = new URL(window.location.href);
    url.search = next === "chat" ? "" : `?mode=${next}`;
    window.history.replaceState(null, "", url);
    setMode(next);
  }

  return (
    <>
      <nav className="mode-switch" aria-label="Workflow">
        {modes.map(([item, label]) => (
          <button
            key={item}
            type="button"
            aria-current={mode === item ? "page" : undefined}
            onClick={() => switchMode(item)}
          >
            {label}
          </button>
        ))}
      </nav>
      {mode === "chat" && <App />}
      {mode === "requirements" && <Requirements />}
      {mode === "api-design" && <ApiDesign />}
      {mode === "workflows" && <Workflows />}
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Shell />
  </React.StrictMode>,
);
