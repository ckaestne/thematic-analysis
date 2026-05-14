import { Badge, type MantineColor } from "@mantine/core";

const colors: Record<string, MantineColor> = {
  pending: "gray",
  coding: "blue",
  running: "blue",
  aggregating: "violet",
  reviewing: "yellow",
  done: "teal",
  failed: "red",
  applied: "teal",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  const s = (status ?? "unknown").toLowerCase();
  return (
    <Badge color={colors[s] ?? "gray"} variant="light" tt="lowercase">
      {s}
    </Badge>
  );
}
