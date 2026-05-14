import { Card, Group, Stack, Text, type MantineColor } from "@mantine/core";
import type { ReactNode } from "react";

export function StatCard({
  label,
  value,
  hint,
  icon,
  color = "indigo",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  color?: MantineColor;
}) {
  return (
    <Card padding="md">
      <Group justify="space-between" wrap="nowrap">
        <Stack gap={2}>
          <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
            {label}
          </Text>
          <Text size="xl" fw={700} c={color}>
            {value}
          </Text>
          {hint ? (
            <Text size="xs" c="dimmed">
              {hint}
            </Text>
          ) : null}
        </Stack>
        {icon ? <div style={{ color: "var(--mantine-color-dimmed)" }}>{icon}</div> : null}
      </Group>
    </Card>
  );
}
