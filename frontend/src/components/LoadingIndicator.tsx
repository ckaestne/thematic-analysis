import { Group, Loader, Text } from "@mantine/core";

export function LoadingIndicator({
  label = "Loading…",
  size = "sm",
}: {
  label?: string;
  size?: "xs" | "sm" | "md" | "lg" | "xl";
}) {
  return (
    <Group gap="xs" align="center">
      <Loader size={size} />
      <Text c="dimmed" size="sm">
        {label}
      </Text>
    </Group>
  );
}
