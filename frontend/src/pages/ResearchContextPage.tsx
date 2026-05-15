import {
  Accordion,
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Stack,
  Text,
  Textarea,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { notifications } from "@mantine/notifications";
import { api } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";

const ROLE_LABELS: Record<string, string> = {
  coder: "Coder",
  coding_critic: "Coding critic",
  theme_coder: "Theme coder",
  reviewer: "Reviewer",
  theme_aggregator: "Theme aggregator",
};

const DEFAULT_ROLES = [
  "coder",
  "coding_critic",
  "theme_coder",
  "reviewer",
  "theme_aggregator",
];

export function ResearchContextPage() {
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["research-context"],
    queryFn: api.researchContext,
  });

  const [description, setDescription] = useState("");
  const [tailored, setTailored] = useState<Record<string, string>>({});
  const [savedDescription, setSavedDescription] = useState("");

  useEffect(() => {
    if (data) {
      setDescription(data.description);
      setSavedDescription(data.description);
      setTailored(data.tailored_prompts ?? {});
    } else if (data === null) {
      setDescription("");
      setSavedDescription("");
      setTailored({});
    }
  }, [data]);

  const save = useMutation({
    mutationFn: () =>
      api.putResearchContext({
        description,
        tailored_prompts: tailored,
      }),
    onSuccess: (resp) => {
      notifications.show({
        message: "Research context saved",
        color: "teal",
      });
      setSavedDescription(resp.description);
      setTailored(resp.tailored_prompts ?? {});
      qc.invalidateQueries({ queryKey: ["research-context"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  const regenerate = useMutation({
    mutationFn: async () => {
      // Persist the latest description first so the server regenerates
      // from what the user actually wrote.
      await api.putResearchContext({
        description,
        tailored_prompts: tailored,
      });
      return api.regenerateTailoredPrompts();
    },
    onSuccess: (resp) => {
      notifications.show({
        message: "Tailored prompts regenerated",
        color: "teal",
      });
      setSavedDescription(resp.description);
      setTailored(resp.tailored_prompts ?? {});
      qc.invalidateQueries({ queryKey: ["research-context"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  if (error) return <ErrorAlert error={error} />;

  const roles = data?.roles ?? DEFAULT_ROLES;
  const busy = isLoading || save.isPending || regenerate.isPending;
  const dirty = description !== savedDescription;
  const hasTailored = Object.values(tailored).some((v) => v && v.trim());
  const stale = hasTailored && dirty;

  const updateTailored = (role: string, val: string) =>
    setTailored((t) => ({ ...t, [role]: val }));

  return (
    <Stack gap="md" maw={900}>
      <Title order={2}>Research context</Title>

      <Card padding="lg">
        <Stack gap="md">
          <Textarea
            label="Description"
            description={
              "Freeform statement of the research context and research " +
              "question(s). This will be injected into every agent's prompt; " +
              "you can also generate per-agent tailored versions below."
            }
            autosize
            minRows={8}
            value={description}
            disabled={busy}
            onChange={(e) => setDescription(e.currentTarget.value)}
          />

          {stale && (
            <Alert color="yellow" variant="light">
              The description has changed since the tailored prompts were
              generated — they may be out of sync. Click "Regenerate
              tailored prompts" to refresh them.
            </Alert>
          )}

          <Group justify="flex-end">
            <Button
              variant="default"
              loading={save.isPending}
              onClick={() => save.mutate()}
              disabled={busy && !save.isPending}
            >
              Save
            </Button>
            <Button
              loading={regenerate.isPending}
              onClick={() => regenerate.mutate()}
              disabled={(busy && !regenerate.isPending) || !description.trim()}
            >
              Save & regenerate tailored prompts
            </Button>
          </Group>
        </Stack>
      </Card>

      <Card padding="lg">
        <Stack gap="sm">
          <Group justify="space-between">
            <Title order={4}>Tailored prompts per agent</Title>
            <Badge color={hasTailored ? "teal" : "gray"} variant="light">
              {hasTailored
                ? `${Object.values(tailored).filter((v) => v && v.trim()).length} / ${roles.length} set`
                : "none generated"}
            </Badge>
          </Group>
          <Text size="sm" c="dimmed">
            Each role gets a prompt section authored for its specific job
            (coding, theme building, review, aggregation). Generated from
            the description above by an LLM; you can edit by hand.
          </Text>
          <Accordion variant="separated" multiple>
            {roles.map((role) => (
              <Accordion.Item key={role} value={role}>
                <Accordion.Control>
                  <Group gap="sm">
                    <Text fw={500}>{ROLE_LABELS[role] ?? role}</Text>
                    {tailored[role]?.trim() ? (
                      <Badge size="xs" color="teal" variant="light">
                        set
                      </Badge>
                    ) : (
                      <Badge size="xs" color="gray" variant="light">
                        empty
                      </Badge>
                    )}
                  </Group>
                </Accordion.Control>
                <Accordion.Panel>
                  <Textarea
                    autosize
                    minRows={6}
                    value={tailored[role] ?? ""}
                    disabled={busy}
                    placeholder={
                      "Empty — falls back to the raw description. Click " +
                      "Regenerate above or paste a tailored prompt here."
                    }
                    onChange={(e) =>
                      updateTailored(role, e.currentTarget.value)
                    }
                  />
                </Accordion.Panel>
              </Accordion.Item>
            ))}
          </Accordion>
        </Stack>
      </Card>
    </Stack>
  );
}
