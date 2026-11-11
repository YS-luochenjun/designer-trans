import type { SelectHTMLAttributes } from "react";
import "./Select.css";

/** Native semantics preserve form validation, labels and platform keyboard support. */
export function Select({ className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={`workbench-select ${className}`.trim()} />;
}
