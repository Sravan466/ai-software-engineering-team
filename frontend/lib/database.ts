import type { Project } from "@/lib/api";

/**
 * A build that reads a database nobody gave it: asked, and answered "later" (or
 * removed since). The one rule behind every "Database not connected" mark — the
 * build header, the sidebar row, the Ship review — so they can never disagree.
 */
export function databaseUnconnected(project: Pick<Project, "charter" | "database_status">): boolean {
  return Boolean(project.charter?.env?.length) && project.database_status === "later";
}
