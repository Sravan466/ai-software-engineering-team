import type { Project } from "@/lib/api";

/**
 * App connectors a build uses that were answered and still aren't connected — put
 * off till later, or a key that failed its last test. Computed by the server from
 * the live state (`connectors_unconnected`), so connecting one in the Connectors tab
 * clears every build's mark at once. The one rule behind every "N connectors not
 * connected" mark: the build header, the sidebar row, the Ship review.
 */
export function connectorsUnconnected(project: Pick<Project, "connectors_unconnected">): string[] {
  return project.connectors_unconnected ?? [];
}

export function connectorsLabel(n: number): string {
  return `${n} ${n === 1 ? "connector" : "connectors"} not connected`;
}
