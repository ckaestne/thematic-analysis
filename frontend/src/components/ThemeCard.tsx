import {
  Anchor,
  Badge,
  Button,
  Card,
  Code as CodeText,
  Group,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { IconArrowBackUp, IconTrash } from "@tabler/icons-react";
import { Link } from "react-router-dom";
import type { ThemeFull } from "../api";
import { QuoteLink } from "./QuoteLink";

const SOURCE_COLOR: Record<string, string> = {
  job: "blue",
  aggregator: "teal",
  manual: "grape",
};

export function ThemeCard({
  theme,
  onDelete,
  onRestore,
  showSourceBadge = false,
  showJobLink = false,
}: {
  theme: ThemeFull;
  onDelete?: () => void;
  onRestore?: () => void;
  showSourceBadge?: boolean;
  showJobLink?: boolean;
}) {
  const dim = theme.deleted;
  return (
    <Card
      padding="md"
      withBorder
      style={{
        opacity: dim ? 0.55 : 1,
        background: dim ? "var(--mantine-color-gray-0)" : undefined,
      }}
    >
      <Stack gap="xs">
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
            <Group gap="xs" wrap="nowrap">
              <Anchor
                component={Link}
                to={`/theme/${theme.theme_id}`}
                style={{ minWidth: 0 }}
              >
                <Title order={4} style={{ margin: 0 }}>
                  {theme.title}
                </Title>
              </Anchor>
              {dim && (
                <Badge color="gray" variant="light">
                  deleted
                </Badge>
              )}
              {showSourceBadge && (
                <Badge
                  color={SOURCE_COLOR[theme.source] ?? "gray"}
                  variant="light"
                >
                  {theme.source}
                </Badge>
              )}
              {showJobLink && theme.theme_coding_job_id != null && (
                <Anchor
                  component={Link}
                  to={`/theme-coding-jobs/${theme.theme_coding_job_id}`}
                  size="xs"
                >
                  from job #{theme.theme_coding_job_id}
                </Anchor>
              )}
            </Group>
            {theme.description && <Text size="sm">{theme.description}</Text>}
            {theme.rationale && (
              <Text size="sm" c="dimmed" fs="italic">
                Rationale: {theme.rationale}
              </Text>
            )}
          </Stack>
          <Group gap="xs">
            {!dim && onDelete && (
              <Button
                size="xs"
                color="red"
                variant="subtle"
                leftSection={<IconTrash size={14} />}
                onClick={onDelete}
              >
                Delete
              </Button>
            )}
            {dim && onRestore && (
              <Button
                size="xs"
                variant="subtle"
                leftSection={<IconArrowBackUp size={14} />}
                onClick={onRestore}
              >
                Restore
              </Button>
            )}
          </Group>
        </Group>

        {theme.codes.length > 0 && (
          <div>
            <Text size="xs" c="dimmed" mb={4}>
              Codes ({theme.codes.length})
            </Text>
            <Group gap={6} wrap="wrap">
              {theme.codes.map((c) => (
                <Anchor
                  key={c.code_id}
                  component={Link}
                  to={`/code/${c.code_id}`}
                >
                  <CodeText style={{ fontSize: 13 }}>{c.code}</CodeText>
                </Anchor>
              ))}
            </Group>
          </div>
        )}

        {theme.quotes.length > 0 && (
          <div>
            <Text size="xs" c="dimmed" mb={4}>
              Supporting quotes ({theme.quotes.length})
            </Text>
            <Stack gap={4}>
              {theme.quotes.map((q) => (
                <QuoteLink key={q.quote_id} q={q} />
              ))}
            </Stack>
          </div>
        )}
      </Stack>
    </Card>
  );
}
