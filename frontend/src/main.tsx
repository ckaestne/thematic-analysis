import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";

import React from "react";
import ReactDOM from "react-dom/client";
import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { theme } from "./theme";
import { DevErrorBoundary } from "./components/DevErrorBoundary";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Auto-refresh every 5s so partial progress is visible without a refresh.
      refetchInterval: 5000,
      refetchOnWindowFocus: true,
      staleTime: 1000,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="auto">
      <Notifications position="top-right" />
      <QueryClientProvider client={queryClient}>
        <BrowserRouter basename={new URL(document.baseURI).pathname.replace(/\/$/, "")}>
          <DevErrorBoundary>
            <App />
          </DevErrorBoundary>
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  </React.StrictMode>,
);
