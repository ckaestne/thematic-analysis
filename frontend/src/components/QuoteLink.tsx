import { Anchor, Text } from "@mantine/core";
import { Link } from "react-router-dom";

export type QuoteRefLike = {
  quote_id: number;
  text: string;
  segment_id: number | null;
  document_id: number | null;
  document_filename: string | null;
};

export function QuoteLink({ q }: { q: QuoteRefLike }) {
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
      <Text size="xs" ff="monospace">
        {q.document_filename ?? "(no document)"}#{q.segment_id}
        <Text span size="xs" c="dimmed">
          {" "}
          · quote {q.quote_id}
        </Text>
      </Text>
      <Text size="sm">{q.text}</Text>
    </Anchor>
  );
}
