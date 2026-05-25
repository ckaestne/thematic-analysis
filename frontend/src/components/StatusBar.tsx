import { Group, Progress, Stack, Text, Tooltip } from "@mantine/core";

const colors: Record<string, string> = {
  pending: "gray",
  coding: "blue",
  running: "blue",
  aggregating: "violet",
  reviewing: "yellow",
  done: "teal",
  failed: "red",
  applied: "teal",
  fully_coded: "teal",
  "fully coded": "teal",
  partially_coded: "blue",
  "partially coded": "blue",
  not_coded: "gray",
  "not coded": "gray",
};

export function StatusBar({
  counts,
  total,
  height = 12,
}: {
  counts: Record<string, number>;
  total?: number;
  height?: number;
}) {
  const sum = total ?? Object.values(counts).reduce((a, b) => a + b, 0);
  const entries = Object.entries(counts).filter(([, n]) => n > 0);
  if (sum === 0) {
    return (
      <Text size="xs" c="dimmed">
        no data
      </Text>
    );
  }
  return (
    <Stack gap={4}>
      <Progress.Root size={height} radius="sm">
        {entries.map(([k, n]) => (
          <Tooltip key={k} label={`${k}: ${n}`} withinPortal>
            <Progress.Section value={(n / sum) * 100} color={colors[k] ?? "gray"} />
          </Tooltip>
        ))}
      </Progress.Root>
      <Group gap="xs" wrap="wrap">
        {entries.map(([k, n]) => (
          <Text key={k} size="xs" c="dimmed">
            <span
              style={{
                display: "inline-block",
                width: 8,
                height: 8,
                background: `var(--mantine-color-${colors[k] ?? "gray"}-6)`,
                borderRadius: 2,
                marginRight: 4,
              }}
            />
            {k}: {n}
          </Text>
        ))}
      </Group>
    </Stack>
  );
}
