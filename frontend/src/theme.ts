import { createTheme } from "@mantine/core";

export const theme = createTheme({
  primaryColor: "indigo",
  defaultRadius: "md",
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  headings: {
    fontWeight: "600",
  },
  components: {
    Card: {
      defaultProps: {
        withBorder: true,
        shadow: "xs",
      },
    },
    Badge: {
      defaultProps: {
        radius: "sm",
      },
    },
  },
});
