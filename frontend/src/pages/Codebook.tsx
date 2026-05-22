import {
  Anchor,
  Badge,
  Button,
  Card,
  Code,
  Group,
  Select,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { IconGitBranch } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type CodebookCode, type CodebookQuote } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";

const QUOTES_VISIBLE = 3;

function QuoteLink({ q }: { q: CodebookQuote }) {
  const to = q.document_id
    ? `/documents/${q.document_id}?segment=${q.segment_id}&quote=${q.quote_id}`
    : `/segments/${q.segment_id}?quote=${q.quote_id}`;
  return (
    <Anchor
      component={Link}
      to={to}
      style={{
        display: "block",
        borderLeft: "3px solid var(--mantine-color-indigo-6)",
        padding: "4px 10px",
        background: "var(--mantine-color-default-hover)",
        borderRadius: 3,
        textDecoration: "none",
        color: "inherit",
      }}
    >
      <Text size="xs" c="dimmed" ff="monospace">
        quote {q.quote_id} · segment {q.segment_id}
      </Text>
      <Text size="sm">{q.text}</Text>
    </Anchor>
  );
}

function CodeCard({ c }: { c: CodebookCode }) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? c.quotes : c.quotes.slice(0, QUOTES_VISIBLE);
  const hidden = c.quotes.length - visible.length;
  return (
    <Card padding="md" withBorder>
      <Group justify="space-between" mb="xs" align="flex-start">
        <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
          <Code style={{ fontSize: 15, fontWeight: 600 }}>{c.code}</Code>
          {c.description && (
            <Text size="sm" c="dimmed">
              {c.description}
            </Text>
          )}
        </Stack>
        <Group gap="xs" wrap="nowrap">
          <Text size="xs" c="dimmed">
            {c.quotes.length} quote{c.quotes.length === 1 ? "" : "s"}
          </Text>
          <Button
            component={Link}
            to={`/code/${c.code_id}`}
            variant="light"
            size="xs"
            leftSection={<IconGitBranch size={14} />}
          >
            Lineage
          </Button>
        </Group>
      </Group>
      <Stack gap={6}>
        {visible.map((q) => (
          <QuoteLink key={q.quote_id} q={q} />
        ))}
        {hidden > 0 && (
          <Button
            variant="subtle"
            size="xs"
            onClick={() => setShowAll(true)}
            style={{ alignSelf: "flex-start" }}
          >
            Show {hidden} more quote{hidden === 1 ? "" : "s"}
          </Button>
        )}
        {showAll && c.quotes.length > QUOTES_VISIBLE && (
          <Button
            variant="subtle"
            size="xs"
            onClick={() => setShowAll(false)}
            style={{ alignSelf: "flex-start" }}
          >
            Show fewer
          </Button>
        )}
      </Stack>
    </Card>
  );
}

export function CodebookPage() {
  const navigate = useNavigate();
  const { version: versionParam } = useParams();
  const versions = useQuery({
    queryKey: ["codebook-versions"],
    queryFn: api.codebookVersions,
  });

  // Latest is the highest version number.
  const latestVersion = versions.data
    ? versions.data
        .map((v) => v.version)
        .reduce((a, b) => (a > b ? a : b), 0)
    : undefined;

  const selectedVersion = versionParam
    ? parseInt(versionParam, 10)
    : latestVersion;

  const detail = useQuery({
    queryKey: ["codebook-version", selectedVersion],
    queryFn: () => api.codebookVersion(selectedVersion!),
    enabled: !!selectedVersion,
  });

  useEffect(() => {
    if (!versionParam && latestVersion) {
      navigate(`/codebook/${latestVersion}`, { replace: true });
    }
  }, [versionParam, latestVersion, navigate]);

  if (versions.error) return <ErrorAlert error={versions.error} />;

  const versionOptions = (versions.data ?? [])
    .slice()
    .sort((a, b) => b.version - a.version)
    .map((v) => ({
      value: String(v.version),
      label: `v${v.version} · ${v.n_codes} codes${
        v.version === latestVersion ? " · latest" : ""
      }${v.created_at ? ` · ${v.created_at.slice(0, 10)}` : ""}`,
    }));

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <Title order={2}>Codebook</Title>
        <Group gap="sm">
          <Select
            label="Version"
            placeholder="Select version"
            data={versionOptions}
            value={selectedVersion ? String(selectedVersion) : null}
            onChange={(v) => v && navigate(`/codebook/${v}`)}
            w={320}
            searchable
            allowDeselect={false}
          />
        </Group>
      </Group>

      {detail.error ? (
        <ErrorAlert error={detail.error} />
      ) : !detail.data ? (
        <Text c="dimmed">Select a version.</Text>
      ) : (
        <Stack gap="sm">
          <Card padding="md" withBorder>
            <Group justify="space-between">
              <Stack gap={2}>
                <Title order={3}>Version {detail.data.version}</Title>
                <Text size="xs" c="dimmed">
                  {detail.data.created_at} · research context v
                  {detail.data.research_context_version}
                  {detail.data.parent_version != null && (
                    <>
                      {" "}
                      · parent{" "}
                      <Anchor
                        component={Link}
                        to={`/codebook/${detail.data.parent_version}`}
                        size="xs"
                      >
                        v{detail.data.parent_version}
                      </Anchor>
                    </>
                  )}
                </Text>
              </Stack>
              <Badge variant="light" size="lg">
                {detail.data.codes.length} codes
              </Badge>
            </Group>
          </Card>

          {detail.data.codes.length === 0 ? (
            <Card padding="md" withBorder>
              <Text c="dimmed" ta="center" py="md">
                Empty codebook.
              </Text>
            </Card>
          ) : (
            detail.data.codes.map((c) => (
              <CodeCard key={c.code_id} c={c} />
            ))
          )}
        </Stack>
      )}
    </Stack>
  );
}
