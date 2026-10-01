import type { Project } from "@/lib/api";

/**
 * App connectors a build uses and was told to add later — the one rule behind every
 * "N connectors not connected" mark (the build header, the sidebar row, the Ship
 * review), the same way `databaseUnconnected` is for the database.
 */
export function connectorsUnconnected(project: Pick<Project, "charter" | "integrations_status">): string[] {
  const used = project.charter?.integrations ?? [];
  const status = project.integrations_status ?? {};
  return used.filter((iid) => status[iid] === "later");
}

export function connectorsLabel(n: number): string {
  return `${n} ${n === 1 ? "connector" : "connectors"} not connected`;
}
