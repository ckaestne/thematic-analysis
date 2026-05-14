import { Button, Group, Modal, Text } from "@mantine/core";
import { useState, type ReactNode } from "react";

export function useConfirmDelete() {
  const [state, setState] = useState<{
    open: boolean;
    title: string;
    body: ReactNode;
    onConfirm: () => Promise<unknown> | void;
  }>({
    open: false,
    title: "",
    body: null,
    onConfirm: () => {},
  });
  const [loading, setLoading] = useState(false);

  const ask = (opts: {
    title: string;
    body: ReactNode;
    onConfirm: () => Promise<unknown> | void;
  }) => setState({ open: true, ...opts });

  const close = () => setState((s) => ({ ...s, open: false }));

  const modal = (
    <Modal opened={state.open} onClose={close} title={state.title} centered>
      <Text size="sm" mb="md">
        {state.body}
      </Text>
      <Group justify="flex-end">
        <Button variant="default" onClick={close} disabled={loading}>
          Cancel
        </Button>
        <Button
          color="red"
          loading={loading}
          onClick={async () => {
            try {
              setLoading(true);
              await state.onConfirm();
              close();
            } finally {
              setLoading(false);
            }
          }}
        >
          Delete
        </Button>
      </Group>
    </Modal>
  );

  return { ask, modal };
}
