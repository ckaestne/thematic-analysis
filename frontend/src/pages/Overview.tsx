import {
  Anchor,
  Card,
  Code,
  Group,
  SimpleGrid,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import {
  IconBook2,
  IconBulb,
  IconDatabase,
  IconFile,
  IconMessage,
  IconTag,
} from "@tabler/icons-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";
import { StatCard } from "../components/StatCard";
import { StatusBar } from "../components/StatusBar";

function bytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function Overview() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
  });

  if (error) return <ErrorAlert error={error} />;
  if (isLoading || !data) return <Text c="dimmed">Loading…</Text>;

  const { stage1 } = data;
  const desc = data.research_context_description.trim();

  const codebookProgress = {
    pending: stage1.reviews_pending,
    done: stage1.reviews_completed_since_codebook,
  };
  const codebookTotal =
    codebookProgress.pending + codebookProgress.done;

  return (
    <Stack gap="md">
      <Group justify="space-between" align="end">
        <div>
          <Title order={2}>Overview</Title>
          <Text c="dimmed" size="sm">
            <Code>{data.db_path}</Code> · {bytes(data.db_size_bytes)}
          </Text>
        </div>
      </Group>

      <Card padding="md" withBorder>
        <Group justify="space-between" mb={4} wrap="nowrap">
          <Title order={4}>Research context</Title>
          <Anchor component={Link} to="/research-context" size="sm">
            {data.research_context_set ? "edit" : "set up"}
          </Anchor>
        </Group>
        {desc ? (
          <Text style={{ whiteSpace: "pre-wrap" }}>{desc}</Text>
        ) : (
          <Text c="orange" size="sm">
            No research context has been set. The agents will fall back to
            generic prompts. Click <em>set up</em> to add one.
          </Text>
        )}
      </Card>

      <SimpleGrid cols={{ base: 2, sm: 3, md: 6 }} spacing="md">
        <StatCard
          label="Documents"
          value={stage1.documents_total}
          icon={<IconFile size={28} />}
        />
        <StatCard
          label="Segments"
          value={stage1.segments_total}
          icon={<IconDatabase size={28} />}
        />
        <StatCard
          label="Codes"
          value={stage1.codes_total}
          icon={<IconTag size={28} />}
        />
        <StatCard
          label="Quotes"
          value={stage1.quotes_total}
          icon={<IconMessage size={28} />}
        />
        <StatCard
          label="Codebook"
          value={`v${stage1.codebook_version}`}
          hint={`${stage1.codebook_codes} codes`}
          icon={<IconBook2 size={28} />}
        />
        <StatCard
          label="Themes"
          value={stage1.themes_total}
          icon={<IconBulb size={28} />}
        />
      </SimpleGrid>

      <Card padding="md">
        <Stack gap="sm">
          <Group justify="space-between">
            <Title order={4}>Document coding progress</Title>
            <Text size="xs" c="dimmed">
              {stage1.documents_total} document
              {stage1.documents_total === 1 ? "" : "s"}
            </Text>
          </Group>
          <StatusBar
            counts={{
              "fully coded":
                stage1.documents_by_coding_status.fully_coded ?? 0,
              "partially coded":
                stage1.documents_by_coding_status.partially_coded ?? 0,
              "not coded":
                stage1.documents_by_coding_status.not_coded ?? 0,
            }}
          />
          <Text size="xs" c="dimmed">
            A document is fully coded when every segment has aggregated
            codes; partially coded when any segment has any coder codes
            but the document is not yet fully aggregated.
          </Text>
        </Stack>
      </Card>

      <Card padding="md">
        <Stack gap="sm">
          <Group justify="space-between">
            <Title order={4}>Codebook progress</Title>
            <Text size="xs" c="dimmed">
              since v{stage1.codebook_version}
            </Text>
          </Group>
          {codebookTotal === 0 ? (
            <Text size="sm" c="dimmed">
              No reviews pending or completed since the latest codebook
              revision.
            </Text>
          ) : (
            <StatusBar
              counts={{
                pending: codebookProgress.pending,
                done: codebookProgress.done,
              }}
            />
          )}
          <Text size="xs" c="dimmed">
            Reviews completed since the last codebook revision will be
            integrated when the codebook is next materialized.
          </Text>
        </Stack>
      </Card>
    </Stack>
  );
}
