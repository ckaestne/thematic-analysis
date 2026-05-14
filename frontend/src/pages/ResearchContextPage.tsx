import {
  Button,
  Card,
  Group,
  Stack,
  TagsInput,
  Textarea,
  TextInput,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { notifications } from "@mantine/notifications";
import { api, type ResearchContext } from "../api";
import { ErrorAlert } from "../components/ErrorAlert";

const empty: ResearchContext = {
  title: "",
  aim: "",
  research_questions: [],
  theoretical_framework: "",
  paradigm: "",
  methodology: "thematic_analysis",
  domain: "",
  background: "",
  keywords: [],
};

export function ResearchContextPage() {
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["research-context"],
    queryFn: api.researchContext,
  });

  const [form, setForm] = useState<ResearchContext>(empty);
  useEffect(() => {
    if (data) setForm(data);
  }, [data]);

  const save = useMutation({
    mutationFn: (v: ResearchContext) => api.putResearchContext(v),
    onSuccess: () => {
      notifications.show({
        message: "Research context saved",
        color: "teal",
      });
      qc.invalidateQueries({ queryKey: ["research-context"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (e) =>
      notifications.show({
        message: e instanceof Error ? e.message : String(e),
        color: "red",
      }),
  });

  if (error) return <ErrorAlert error={error} />;

  const set = <K extends keyof ResearchContext>(
    key: K,
    val: ResearchContext[K],
  ) => setForm((f) => ({ ...f, [key]: val }));

  return (
    <Stack gap="md" maw={900}>
      <Title order={2}>Research context</Title>
      <Card padding="lg">
        <Stack gap="md">
          <TextInput
            label="Title"
            value={form.title}
            disabled={isLoading || save.isPending}
            onChange={(e) => set("title", e.currentTarget.value)}
          />
          <Textarea
            label="Aim"
            description="Primary aim or purpose of the research."
            autosize
            minRows={2}
            value={form.aim}
            disabled={isLoading || save.isPending}
            onChange={(e) => set("aim", e.currentTarget.value)}
          />
          <TagsInput
            label="Research questions"
            description="Press Enter after each question."
            value={form.research_questions}
            onChange={(v) => set("research_questions", v)}
            disabled={isLoading || save.isPending}
          />
          <Group grow>
            <TextInput
              label="Theoretical framework"
              value={form.theoretical_framework}
              onChange={(e) => set("theoretical_framework", e.currentTarget.value)}
              disabled={isLoading || save.isPending}
            />
            <TextInput
              label="Paradigm"
              value={form.paradigm}
              onChange={(e) => set("paradigm", e.currentTarget.value)}
              disabled={isLoading || save.isPending}
            />
          </Group>
          <Group grow>
            <TextInput
              label="Methodology"
              value={form.methodology}
              onChange={(e) => set("methodology", e.currentTarget.value)}
              disabled={isLoading || save.isPending}
            />
            <TextInput
              label="Domain"
              value={form.domain}
              onChange={(e) => set("domain", e.currentTarget.value)}
              disabled={isLoading || save.isPending}
            />
          </Group>
          <Textarea
            label="Background"
            autosize
            minRows={3}
            value={form.background}
            onChange={(e) => set("background", e.currentTarget.value)}
            disabled={isLoading || save.isPending}
          />
          <TagsInput
            label="Keywords"
            value={form.keywords}
            onChange={(v) => set("keywords", v)}
            disabled={isLoading || save.isPending}
          />
          <Group justify="flex-end">
            <Button
              loading={save.isPending}
              onClick={() => save.mutate(form)}
              disabled={isLoading}
            >
              Save
            </Button>
          </Group>
        </Stack>
      </Card>
    </Stack>
  );
}
