export function workflowLabel(label: string) {
  return label.replace(/^\s*[①-⑳]\s*/u, "");
}
