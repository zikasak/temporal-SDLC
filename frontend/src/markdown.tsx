import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

export const markdownComponents: Components = {
  a: ({ node: _node, ...props }) => (
    <a {...props} target="_blank" rel="noopener noreferrer" />
  ),
};
export const remarkPlugins = [remarkGfm];
