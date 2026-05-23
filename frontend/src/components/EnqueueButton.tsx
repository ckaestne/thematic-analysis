import { ActionIcon, Tooltip } from "@mantine/core";
import { IconPlayerPlay } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import type { QueueState } from "../api";

type Props = {
  queueState: QueueState;
  isLoading: boolean;
  onEnqueue: () => void;
  /** Used in the hover tooltip ("Enqueue this <kind>…") and in the
   * earlier-codebook confirmation popup. */
  kind: "document" | "segment";
  /** Override for the normal-state hover tooltip. */
  hoverLabel?: string;
};

export function EnqueueButton({
  queueState,
  isLoading,
  onEnqueue,
  kind,
  hoverLabel,
}: Props) {
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 5000);
    return () => clearTimeout(t);
  }, [armed]);

  if (queueState === "latest") return null;

  if (queueState === "earlier") {
    return (
      <Tooltip
        opened={armed}
        label={`This ${kind} has already been queued for an earlier codebook. Click again to queue it with the latest codebook.`}
        withArrow
        multiline
        w={280}
        position="left"
      >
        <ActionIcon
          variant="subtle"
          color="gray"
          loading={isLoading}
          onClick={() => {
            if (armed) {
              setArmed(false);
              onEnqueue();
            } else {
              setArmed(true);
            }
          }}
        >
          <IconPlayerPlay size={16} />
        </ActionIcon>
      </Tooltip>
    );
  }

  return (
    <Tooltip
      label={
        hoverLabel ??
        `Enqueue this ${kind} for coding by all registered coders (at the latest codebook + research-context revisions)`
      }
    >
      <ActionIcon
        variant="subtle"
        color="blue"
        loading={isLoading}
        onClick={onEnqueue}
      >
        <IconPlayerPlay size={16} />
      </ActionIcon>
    </Tooltip>
  );
}
