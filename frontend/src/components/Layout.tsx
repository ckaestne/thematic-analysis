import { AppShell, Burger, Group, NavLink, ScrollArea, Title } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import {
  IconBook2,
  IconCategory,
  IconChartBar,
  IconClipboardCheck,
  IconCode,
  IconDatabase,
  IconFile,
  IconFlag,
  IconLayoutDashboard,
  IconStack2,
  IconUsers,
  IconUsersGroup,
} from "@tabler/icons-react";
import { NavLink as RouterLink, useLocation } from "react-router-dom";
import type { ReactNode } from "react";

const navItems = [
  { to: "/", label: "Overview", icon: IconLayoutDashboard, end: true },
  { to: "/research-context", label: "Research context", icon: IconFlag },
  { to: "/documents", label: "Documents", icon: IconFile },
  { to: "/segments", label: "Segments", icon: IconDatabase },
  { to: "/coders", label: "Coders", icon: IconUsers },
  { to: "/coder-runs", label: "Coder runs", icon: IconCode },
  { to: "/aggregations", label: "Aggregations", icon: IconStack2 },
  { to: "/review-decisions", label: "Review decisions", icon: IconClipboardCheck },
  { to: "/codebook", label: "Codebook", icon: IconBook2 },
  { to: "/theme-coders", label: "Theme coders", icon: IconUsersGroup },
  { to: "/themes", label: "Themes", icon: IconCategory },
];

export function AppLayout({ children }: { children: ReactNode }) {
  const [opened, { toggle }] = useDisclosure();
  const location = useLocation();
  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 240, breakpoint: "sm", collapsed: { mobile: !opened } }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group>
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" />
            <IconChartBar size={22} />
            <Title order={4} fw={600}>
              Thematic Analysis Inspector
            </Title>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        <AppShell.Section grow component={ScrollArea}>
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = item.end
              ? location.pathname === item.to
              : location.pathname.startsWith(item.to);
            return (
              <NavLink
                key={item.to}
                component={RouterLink}
                to={item.to}
                label={item.label}
                leftSection={<Icon size={18} />}
                active={active}
                variant="filled"
              />
            );
          })}
        </AppShell.Section>
      </AppShell.Navbar>
      <AppShell.Main>{children}</AppShell.Main>
    </AppShell>
  );
}
