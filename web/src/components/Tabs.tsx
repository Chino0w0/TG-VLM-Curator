import { type ReactNode, useId } from "react";

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
  return (
    <div>
      <div
        className="tablist"
        role="tablist"
        aria-label={ariaLabel}
        {...({ focusgroup: "tablist nomemory" } as Record<string, string>)}
      >
        {tabs.map((tab) => {
          const selected = tab.key === activeKey;
          const tabId = `${baseId}-tab-${tab.key}`;
          return (
            <button
              key={tab.key}
              id={tabId}
              type="button"
              role="tab"
              className="tab-btn"
              aria-selected={selected}
              onClick={() => onSelect(tab.key)}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {tabs.map((tab) => {
        const selected = tab.key === activeKey;
        const tabId = `${baseId}-tab-${tab.key}`;
        return (
          <div
            key={tab.key}
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
