import { AppShell, Burger, Group, NavLink, ScrollArea, Text, Title } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import {
  IconBook2,
  IconBulb,
  IconChartBar,
  IconFile,
  IconFlag,
  IconLayoutDashboard,
  IconListDetails,
  IconProgressCheck,
  IconUsers,
} from "@tabler/icons-react";
import { NavLink as RouterLink, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import type { Icon } from "@tabler/icons-react";

type NavItem = {
  to: string;
  label: string;
  icon: Icon;
  end?: boolean;
};

type NavGroup = {
  label?: string;
  items: NavItem[];
};

const navGroups: NavGroup[] = [
  {
    items: [{ to: "/", label: "Overview", icon: IconLayoutDashboard, end: true }],
  },
  {
    items: [
      { to: "/research-context", label: "Research context", icon: IconFlag },
      { to: "/documents", label: "Documents", icon: IconFile },
    ],
  },
  {
    label: "Phase 1",
    items: [
      { to: "/coders", label: "Coders", icon: IconUsers },
      {
        to: "/coding-progress",
        label: "Coding progress",
        icon: IconProgressCheck,
      },
      { to: "/codebook", label: "Codebook", icon: IconBook2 },
    ],
  },
  {
    label: "Phase 2",
    items: [
      {
        to: "/theme-coding-jobs",
        label: "Theme coding jobs",
        icon: IconListDetails,
      },
      { to: "/themes", label: "Themes", icon: IconBulb },
    ],
  },
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
          {navGroups.map((group, idx) => (
            <div key={group.label ?? `group-${idx}`} style={{ marginTop: idx === 0 ? 0 : 12 }}>
              {group.label && (
                <Text
                  size="xs"
                  fw={600}
                  tt="uppercase"
                  c="dimmed"
                  px="sm"
                  pb={4}
                >
                  {group.label}
                </Text>
              )}
              {group.items.map((item) => {
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
            </div>
          ))}
        </AppShell.Section>
      </AppShell.Navbar>
      <AppShell.Main>{children}</AppShell.Main>
    </AppShell>
  );
}
