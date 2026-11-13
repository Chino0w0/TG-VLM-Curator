import { Children, type ReactNode, useId } from "react";

export interface ResponsiveTableProps {
  caption: string;
  headers: string[];
  children: ReactNode;
  loading?: boolean;
  emptyMessage?: string;
}

export function ResponsiveTable({
  caption,
  headers,
  children,
  loading = false,
  emptyMessage,
}: ResponsiveTableProps) {
  const captionId = useId();
  const isEmpty = Children.count(children) === 0;
  return (
    <div
      tabIndex={0}
      role="region"
      aria-labelledby={captionId}
      className="table-wrapper"
    >
      <table>
        <caption id={captionId}>{caption}</caption>
        <thead>
          <tr>
            {headers.map((header) => (
              <th key={header} scope="col">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
      {isEmpty && (loading || emptyMessage) ? (
        <p className="table-empty-state stat-meta" aria-live="polite">
          {loading ? "Loading records..." : emptyMessage}
        </p>
      ) : null}
    </div>
  );
}
