import { type ReactNode, useId } from "react";

export interface ResponsiveTableProps {
  caption: string;
  headers: string[];
  children: ReactNode;
}

export function ResponsiveTable({
  caption,
  headers,
  children,
}: ResponsiveTableProps) {
  const captionId = useId();
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
    </div>
  );
}
