import { type KeyboardEvent, type ReactNode, useId, useRef } from "react";

export interface TabDefinition {
  key: string;
  label: string;
  content: ReactNode;
}

export interface TabsProps {
  ariaLabel: string;
  tabs: TabDefinition[];
  activeKey: string;
  onSelect: (key: string) => void;
}

export function Tabs({ ariaLabel, tabs, activeKey, onSelect }: TabsProps) {
  const baseId = useId();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  function handleTabKeyDown(
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) {
    if (tabs.length === 0) return;
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % tabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    tabRefs.current[nextIndex]?.focus();
    onSelect(tabs[nextIndex].key);
  }

  return (
    <div>
      <div
        className="tablist"
        role="tablist"
        aria-label={ariaLabel}
        {...({ focusgroup: "tablist nomemory" } as Record<string, string>)}
      >
        {tabs.map((tab, index) => {
          const selected = tab.key === activeKey;
          const tabId = `${baseId}-tab-${tab.key}`;
          const panelId = `${baseId}-panel-${tab.key}`;
          return (
            <button
              key={tab.key}
              ref={(el) => {
                tabRefs.current[index] = el;
              }}
              id={tabId}
              type="button"
              role="tab"
              className="tab-btn"
              aria-selected={selected}
              aria-controls={panelId}
              tabIndex={selected ? 0 : -1}
              onClick={() => onSelect(tab.key)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {tabs.map((tab) => {
        const selected = tab.key === activeKey;
        const tabId = `${baseId}-tab-${tab.key}`;
        const panelId = `${baseId}-panel-${tab.key}`;
        return (
          <div
            key={tab.key}
            id={panelId}
            role="tabpanel"
            aria-labelledby={tabId}
            hidden={!selected}
          >
            <div className="tabpanel-inner">{selected ? tab.content : null}</div>
          </div>
        );
      })}
    </div>
  );
}
