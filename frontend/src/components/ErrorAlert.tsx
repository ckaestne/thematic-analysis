import { Alert } from "@mantine/core";
import { IconAlertCircle } from "@tabler/icons-react";

export function ErrorAlert({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <Alert
      icon={<IconAlertCircle size={18} />}
      color="red"
      variant="light"
      title="Error"
    >
      {msg}
    </Alert>
  );
}
