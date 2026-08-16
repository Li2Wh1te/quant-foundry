import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

type Theme = "light" | "dark";

const THEME_STORAGE_KEY = "quant-foundry.theme";

function initialTheme(): Theme {
  try {
    const savedTheme = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (savedTheme === "light" || savedTheme === "dark") {
      return savedTheme;
    }
  } catch {
    // Theme persistence is optional when browser storage is unavailable.
  }
  // The workbench is optimized for dense operational data, so dark is the
  // first-visit default. A persisted user decision always takes precedence.
  return "dark";
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // The active theme still applies for the current page lifetime.
    }
  }, [theme]);

  const nextTheme = theme === "light" ? "dark" : "light";
  const label = nextTheme === "dark" ? "切换到深色模式" : "切换到浅色模式";

  return (
    <button
      className="icon-button"
      type="button"
      onClick={() => setTheme(nextTheme)}
      aria-label={label}
      title={label}
    >
      {theme === "light" ? <Moon aria-hidden="true" /> : <Sun aria-hidden="true" />}
    </button>
  );
}
