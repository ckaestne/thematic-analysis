import {
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Card,
  Code as CodeText,
  Group,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { IconArrowBackUp, IconTrash } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type ThemeSummary } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { QuoteLink } from "../components/QuoteLink";
import { useConfirmDelete } from "../components/ConfirmDelete";

const SOURCE_COLOR: Record<string, string> = {
  job: "blue",
  aggregator: "teal",
  manual: "grape",
};

function ThemeRefLine({ t }: { t: ThemeSummary }) {
  return (
    <Card padding="sm" withBorder shadow="none">
      <Group gap="xs" wrap="nowrap" align="flex-start">
        <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
          <Group gap="xs">
            <Anchor component={Link} to={`/themes/${t.theme_id}`}>
              <Text fw={600}>{t.title}</Text>
            </Anchor>
            <Badge size="xs" color={SOURCE_COLOR[t.source] ?? "gray"}>
              {t.source}
            </Badge>
            {t.deleted && (
              <Badge size="xs" color="gray" variant="light">
                deleted
              </Badge>
            )}
          </Group>
          {t.description && (
            <Text size="xs" c="dimmed" lineClamp={2}>
              {t.description}
            </Text>
          )}
        </Stack>
      </Group>
    </Card>
  );
}

export function ThemeDetailPage() {
  const { id } = useParams<{ id: string }>();
  const themeId = Number(id);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { ask, modal } = useConfirmDelete();

  const detail = useQuery({
    queryKey: ["theme", themeId],
    queryFn: () => api.theme(themeId),
    enabled: Number.isFinite(themeId),
  });

  const del = useMutation({
    mutationFn: () => api.deleteTheme(themeId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["theme", themeId] });
      qc.invalidateQueries({ queryKey: ["themes"] });
      qc.invalidateQueries({ queryKey: ["theme-coding-job"] });
      navigate("/themes");
    },
  });

  const restore = useMutation({
    mutationFn: () => api.restoreTheme(themeId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["theme", themeId] });
      qc.invalidateQueries({ queryKey: ["themes"] });
    },
  });

  if (detail.error) return <ErrorAlert error={detail.error} />;
  if (!detail.data) return <Text c="dimmed">Loading…</Text>;
  const d = detail.data;

  return (
    <Stack gap="md">
      <Breadcrumbs>
        <Anchor component={Link} to="/themes">
          Themes
        </Anchor>
        <Text>theme {d.theme_id}</Text>
      </Breadcrumbs>

      <Card
        padding="md"
        withBorder
        style={{
          opacity: d.deleted ? 0.6 : 1,
          background: d.deleted ? "var(--mantine-color-gray-0)" : undefined,
        }}
      >
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <Stack gap={4} style={{ flex: 1, minWidth: 0 }}>
            <Group gap="xs">
              <Title order={2} style={{ margin: 0 }}>
                {d.title}
              </Title>
              <Badge color={SOURCE_COLOR[d.source] ?? "gray"} variant="light">
                {d.source}
              </Badge>
              {d.deleted && (
                <Badge color="gray" variant="light">
                  deleted
                </Badge>
              )}
            </Group>
            {d.description && <Text>{d.description}</Text>}
            {d.rationale && (
              <Text size="sm" c="dimmed" fs="italic">
                Rationale: {d.rationale}
              </Text>
            )}
            <Group gap="md" mt="xs">
              {d.theme_coding_job_id != null && (
                <Anchor
                  component={Link}
                  to={`/theme-coding-jobs/${d.theme_coding_job_id}`}
                  size="sm"
                >
                  from job #{d.theme_coding_job_id}
                </Anchor>
              )}
              {d.codebook_used_id != null && (
                <Anchor
                  component={Link}
                  to={`/codebook/${d.codebook_used_id}`}
                  size="sm"
                >
                  authored against codebook v{d.codebook_used_id}
                </Anchor>
              )}
              {d.created_at && (
                <Text size="xs" c="dimmed">
                  {d.created_at.slice(0, 19).replace("T", " ")}
                </Text>
              )}
            </Group>
          </Stack>
          <Group gap="xs">
            {!d.deleted ? (
              <Button
                color="red"
                variant="light"
                leftSection={<IconTrash size={16} />}
                onClick={() =>
                  ask({
                    title: "Delete theme?",
                    body: (
                      <>
                        Mark <b>{d.title}</b> as deleted? You will be sent
                        back to the Themes page.
                      </>
                    ),
                    onConfirm: () => del.mutateAsync(),
                  })
                }
              >
                Delete
              </Button>
            ) : (
              <Button
                variant="light"
                leftSection={<IconArrowBackUp size={16} />}
                onClick={() => restore.mutate()}
              >
                Restore
              </Button>
            )}
          </Group>
        </Group>
      </Card>

      {d.codes.length > 0 && (
        <Card padding="md" withBorder>
          <Title order={5} mb="xs">
            Codes ({d.codes.length})
          </Title>
          <Stack gap={4}>
            {d.codes.map((c) => (
              <Group key={c.code_id} gap="xs" wrap="nowrap">
                <Anchor component={Link} to={`/code/${c.code_id}`}>
                  <CodeText style={{ fontSize: 14 }}>{c.code}</CodeText>
                </Anchor>
                {c.description && (
                  <Text size="xs" c="dimmed" lineClamp={1}>
                    {c.description}
                  </Text>
                )}
              </Group>
            ))}
          </Stack>
        </Card>
      )}

      {d.quotes.length > 0 && (
        <Card padding="md" withBorder>
          <Title order={5} mb="xs">
            Supporting quotes ({d.quotes.length})
          </Title>
          <Stack gap={4}>
            {d.quotes.map((q) => (
              <QuoteLink key={q.quote_id} q={q} />
            ))}
          </Stack>
        </Card>
      )}

      {(d.derived_from.length > 0 || d.derived_into.length > 0) && (
        <Card padding="md" withBorder>
          <Title order={5} mb="xs">
            Lineage
          </Title>
          {d.derived_from.length > 0 && (
            <Stack gap={4} mb="md">
              <Text size="sm" c="dimmed">
                Derived from ({d.derived_from.length})
              </Text>
              {d.derived_from.map((t) => (
                <ThemeRefLine key={t.theme_id} t={t} />
              ))}
            </Stack>
          )}
          {d.derived_into.length > 0 && (
            <Stack gap={4}>
              <Text size="sm" c="dimmed">
                Folded into ({d.derived_into.length})
              </Text>
              {d.derived_into.map((t) => (
                <ThemeRefLine key={t.theme_id} t={t} />
              ))}
            </Stack>
          )}
        </Card>
      )}
      {modal}
    </Stack>
  );
}
