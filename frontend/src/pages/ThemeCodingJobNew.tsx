import {
  Anchor,
  Breadcrumbs,
  Button,
  Card,
  Code as CodeText,
  Collapse,
  Group,
  Select,
  Stack,
  Text,
  Textarea,
  Title,
  UnstyledButton,
} from "@mantine/core";
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";

export function ThemeCodingJobNewPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();

  const codebooks = useQuery({
    queryKey: ["codebook-versions"],
    queryFn: api.codebookVersions,
  });
  const sysPrompt = useQuery({
    queryKey: ["theme-coder-system-prompt"],
    queryFn: api.themeCoderSystemPrompt,
  });

  const sortedCodebooks = (codebooks.data ?? [])
    .slice()
    .sort((a, b) => b.version - a.version);
  const latestVersion = sortedCodebooks[0]?.version;

  const [codebookVersion, setCodebookVersion] = useState<number | null>(null);
  useEffect(() => {
    if (codebookVersion == null && latestVersion != null) {
      setCodebookVersion(latestVersion);
    }
  }, [codebookVersion, latestVersion]);

  // Prefill the prompt from the chosen codebook's research context. The
  // prefill is sticky: once the user has edited the field manually, we
  // stop overwriting it on codebook-version change.
  const rcVersion = sortedCodebooks.find(
    (c) => c.version === codebookVersion,
  )?.research_context_version;
  const rc = useQuery({
    queryKey: ["research-context-version", rcVersion],
    queryFn: () => api.researchContextVersion(rcVersion!),
    enabled: rcVersion != null,
  });

  const [prompt, setPrompt] = useState("");
  const [dirty, setDirty] = useState(false);
  const prefilled =
    rc.data?.tailored_prompts?.theme_coder?.trim() || rc.data?.description || "";

  useEffect(() => {
    if (!dirty) setPrompt(prefilled);
  }, [prefilled, dirty]);

  const [showSystem, setShowSystem] = useState(false);

  const create = useMutation({
    mutationFn: () =>
      api.createThemeCodingJob({
        codebook_version: codebookVersion!,
        prompt,
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["theme-coding-jobs"] });
      qc.invalidateQueries({ queryKey: ["themes"] });
      navigate(`/theme-coding-jobs/${res.id}`);
    },
  });

  const codebookOptions = sortedCodebooks.map((c) => ({
    value: String(c.version),
    label: `v${c.version} · ${c.n_codes} codes${
      c.version === latestVersion ? " · latest" : ""
    }`,
  }));

  return (
    <Stack gap="md">
      <Breadcrumbs>
        <Anchor component={Link} to="/theme-coding-jobs">
          Theme coding jobs
        </Anchor>
        <Text>new</Text>
      </Breadcrumbs>

      <Title order={2}>New theme coding job</Title>

      <Text c="dimmed" size="sm">
        A theme coding job pins a codebook revision and a researcher
        prompt. Creating it here records those parameters but does not
        invoke the LLM — you'll be sent to the job's page where you can
        run it. (You can also run every pending job at once from the
        list page.) When run, the model receives every code in the
        pinned revision — with its description and supporting quotes —
        and proposes a small set of overarching themes. Your prompt is
        spliced into the user message verbatim, in a clearly-delimited
        block, as the researcher's framing for this specific run.
      </Text>

      <Card padding="md" withBorder>
        <Stack gap="md">
          <Select
            label="Codebook version"
            description="The pinned revision the theme coder will read. Defaults to the latest."
            data={codebookOptions}
            value={codebookVersion != null ? String(codebookVersion) : null}
            onChange={(v) => v && setCodebookVersion(parseInt(v, 10))}
            searchable
            allowDeselect={false}
            w={360}
          />

          <Textarea
            label="Researcher prompt"
            description={
              rc.data
                ? "Pre-filled from this codebook's research context (the theme_coder tailored prompt if present, otherwise the freeform description). Edit freely."
                : "Add the framing the theme coder should adopt: the research question(s), what counts as a meaningful theme for this study, any persona or perspective."
            }
            placeholder="Describe the research focus and what to look for…"
            value={prompt}
            onChange={(e) => {
              setPrompt(e.currentTarget.value);
              setDirty(true);
            }}
            autosize
            minRows={8}
            maxRows={24}
          />

          {dirty && (
            <Group>
              <Button
                size="xs"
                variant="subtle"
                onClick={() => {
                  setPrompt(prefilled);
                  setDirty(false);
                }}
              >
                Reset to research context
              </Button>
            </Group>
          )}

          <div>
            <UnstyledButton onClick={() => setShowSystem((v) => !v)}>
              <Group gap={4} wrap="nowrap">
                {showSystem ? (
                  <IconChevronDown size={16} />
                ) : (
                  <IconChevronRight size={16} />
                )}
                <Text size="sm" fw={500}>
                  How your prompt is combined with the system prompt
                </Text>
              </Group>
            </UnstyledButton>
            <Collapse in={showSystem}>
              <Stack gap="xs" mt="xs">
                <Text size="xs" c="dimmed">
                  The system prompt below is fixed for every job. Your
                  prompt is appended to the user message as the
                  &ldquo;researcher's framing&rdquo;, followed by the
                  codebook in JSON.
                </Text>
                <Text size="xs" fw={600} mt="xs">
                  System prompt
                </Text>
                <CodeText
                  block
                  style={{
                    whiteSpace: "pre-wrap",
                    fontSize: 12,
                  }}
                >
                  {sysPrompt.data?.system_prompt ?? "Loading…"}
                </CodeText>
                <Text size="xs" fw={600} mt="xs">
                  Researcher framing wrapper (user message)
                </Text>
                <CodeText
                  block
                  style={{ whiteSpace: "pre-wrap", fontSize: 12 }}
                >
                  {sysPrompt.data?.user_framing_template ?? "Loading…"}
                </CodeText>
                <Text size="xs" fw={600} mt="xs">
                  Codebook section (user message, appended after the
                  framing)
                </Text>
                <CodeText
                  block
                  style={{ whiteSpace: "pre-wrap", fontSize: 12 }}
                >
                  {sysPrompt.data?.user_codebook_template ?? "Loading…"}
                </CodeText>
              </Stack>
            </Collapse>
          </div>

          {create.error && <ErrorAlert error={create.error} />}

          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={() => navigate("/theme-coding-jobs")}
              disabled={create.isPending}
            >
              Cancel
            </Button>
            <Button
              disabled={
                codebookVersion == null ||
                prompt.trim().length === 0 ||
                create.isPending
              }
              loading={create.isPending}
              onClick={() => create.mutate()}
            >
              Create job
            </Button>
          </Group>
        </Stack>
      </Card>
    </Stack>
  );
}
