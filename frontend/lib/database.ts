import type { Project } from "@/lib/api";

/**
 * A build that reads a database nobody gave it: asked, and answered "later" (or
 * removed since). The one rule behind every "Database not connected" mark — the
 * build header, the sidebar row, the Ship review — so they can never disagree.
 */
export function databaseUnconnected(project: Pick<Project, "charter" | "database_status">): boolean {
  return Boolean(databaseEnv(project).length) && project.database_status === "later";
}

/** The names the database is read from — `env` without the app connectors' (#59). */
export function databaseEnv(project: Pick<Project, "charter">): string[] {
  return project.charter?.database_env ?? project.charter?.env ?? [];
}
