/**
 * One block of the workbench: the console's tab-panel header chrome inside a
 * card, so three blocks that load independently stay visually separate and a
 * block that failed cannot look like part of a block that loaded.
 */

import type { ReactNode } from "react";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import styles from "./index.module.less";

export default function Section({
  icon,
  title,
  wide,
  children,
}: {
  icon: ReactNode;
  title: string;
  /** Span the full workbench row (used by the block whose table is widest). */
  wide?: boolean;
  children: ReactNode;
}) {
  const className = wide ? `${styles.section} ${styles.wide}` : styles.section;
  return (
    <section className={className}>
      <TabPanelHeader icon={icon} title={title} />
      {children}
    </section>
  );
}
