import {
  Accordion,
  ActionIcon,
  Alert,
  Badge,
  Card,
  Code,
  Group,
  Stack,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { IconAlertTriangle, IconSearch, IconTrash } from "@tabler/icons-react";
import { useMemo, useState } from "react";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatusBadge } from "../components/StatusBadge";
import { useConfirmDelete } from "../components/ConfirmDelete";

export function Themes() {
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["theme-aggregation"],
    queryFn: () => api.themeAggregation(),
  });
  const [q, setQ] = useState("");

  const del = useMutation({
    mutationFn: (id: number) => api.deleteThemeAggregation(id),
    onSuccess: () => {
      notifications.show({ message: "Aggregation deleted", color: "teal" });
      qc.invalidateQueries({ queryKey: ["theme-aggregation"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const confirm = useConfirmDelete();

  const themes = useMemo(() => {
    const ts = data?.result?.themes ?? [];
    const needle = q.trim().toLowerCase();
    if (!needle) return ts;
    return ts.filter((t) => {
      const hay = [
        t.name,
        t.description ?? "",
        ...(t.codes ?? []),
        ...(t.original_themes ?? []),
        ...(t.quotes?.map((qq) => qq.text) ?? []),
      ]
        .join(" ")
        .toLowerCase();
      return hay.includes(needle);
    });
  }, [data, q]);

  if (error) return <ErrorAlert error={error} />;
  if (isLoading) return <Text c="dimmed">Loading…</Text>;

  if (!data) {
    return (
      <Stack gap="md">
        <Title order={2}>Themes</Title>
        <Card padding="lg">
          <Text c="dimmed">
            No theme aggregation yet. Run{" "}
            <Code>ta-stage2 theme-code</Code> then{" "}
            <Code>ta-stage2 theme-aggregate</Code> to produce themes.
          </Text>
        </Card>
      </Stack>
    );
  }

  return (
    <Stack gap="md">
      {confirm.modal}
      <Group justify="space-between" align="end">
        <Stack gap={2}>
          <Title order={2}>Themes</Title>
          <Group gap="xs">
            <StatusBadge status={data.status} />
            <Text size="sm" c="dimmed">
              codebook v{data.codebook_version}
            </Text>
            {data.finished_at && (
              <Text size="xs" c="dimmed">
                · {data.finished_at}
              </Text>
            )}
          </Group>
        </Stack>
        <Tooltip label="Delete this aggregation (will be recomputed)">
          <ActionIcon
            variant="light"
            color="red"
            size="lg"
            onClick={() =>
              confirm.ask({
                title: `Delete theme aggregation #${data.id}?`,
                body: "Removes the aggregated themes. ta-stage2 theme-aggregate will rebuild them from the existing theme_coder_runs.",
                onConfirm: () => del.mutateAsync(data.id),
              })
            }
          >
            <IconTrash size={18} />
          </ActionIcon>
        </Tooltip>
      </Group>

      {data.error && (
        <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />}>
          <Code>{data.error}</Code>
        </Alert>
      )}

      {(data.result?.themes?.length ?? 0) > 0 && (
        <TextInput
          placeholder="Filter themes, codes, or quotes…"
          leftSection={<IconSearch size={16} />}
          value={q}
          onChange={(e) => setQ(e.currentTarget.value)}
          maw={420}
        />
      )}

      {themes.length === 0 ? (
        <Card padding="md">
          <Text c="dimmed" ta="center" py="md">
            {data.result?.themes?.length
              ? "No themes match your filter."
              : "No themes in result."}
          </Text>
        </Card>
      ) : (
        <Accordion variant="separated" multiple defaultValue={["t0", "t1", "t2"]}>
          {themes.map((t, i) => (
            <Accordion.Item key={i} value={`t${i}`}>
              <Accordion.Control>
                <Group justify="space-between">
                  <Text fw={600}>{t.name}</Text>
                  <Group gap="xs">
                    <Badge variant="light">{t.codes?.length ?? 0} codes</Badge>
                    <Badge variant="light" color="indigo">
                      {t.quotes?.length ?? 0} quotes
                    </Badge>
                  </Group>
                </Group>
              </Accordion.Control>
              <Accordion.Panel>
                <Stack gap="sm">
                  {t.description && <Text size="sm">{t.description}</Text>}
                  {t.original_themes && t.original_themes.length > 0 && (
                    <div>
                      <Text size="xs" c="dimmed" tt="uppercase" fw={600} mb={4}>
                        Original themes
                      </Text>
                      <Group gap={4}>
                        {t.original_themes.map((ot, j) => (
                          <Badge key={j} variant="light" color="indigo">
                            {ot}
                          </Badge>
                        ))}
                      </Group>
                    </div>
                  )}
                  {t.merge_rationale && (
                    <Card
                      padding="xs"
                      bg="var(--mantine-color-yellow-light)"
                      withBorder={false}
                      shadow="none"
                    >
                      <Text size="xs" c="dimmed" tt="uppercase" fw={600} mb={4}>
                        Merge rationale
                      </Text>
                      <Text size="sm" fs="italic">
                        {t.merge_rationale}
                      </Text>
                    </Card>
                  )}
                  {t.codes && t.codes.length > 0 && (
                    <div>
                      <Text size="xs" c="dimmed" tt="uppercase" fw={600} mb={4}>
                        Codes ({t.codes.length})
                      </Text>
                      <Group gap={4}>
                        {t.codes.map((c, j) => (
                          <Badge key={j} variant="light">
                            {c}
                          </Badge>
                        ))}
                      </Group>
                    </div>
                  )}
                  {t.quotes && t.quotes.length > 0 && (
                    <div>
                      <Text size="xs" c="dimmed" tt="uppercase" fw={600} mb={4}>
                        Quotes ({t.quotes.length})
                      </Text>
                      <Stack gap={4}>
                        {t.quotes.map((q, j) => (
                          <div
                            key={j}
                            style={{
                              borderLeft:
                                "3px solid var(--mantine-color-indigo-6)",
                              padding: "4px 10px",
                              background: "var(--mantine-color-default-hover)",
                              borderRadius: 3,
                            }}
                          >
                            <Text size="xs" c="dimmed" ff="monospace">
                              {q.quote_id}
                            </Text>
                            <Text size="sm">{q.text}</Text>
                          </div>
                        ))}
                      </Stack>
                    </div>
                  )}
                </Stack>
              </Accordion.Panel>
            </Accordion.Item>
          ))}
        </Accordion>
      )}
    </Stack>
  );
}
