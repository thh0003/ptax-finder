import type { LayerStatusValue } from "../api/parcelLayers";
import StatusChip from "./StatusChip";

export default function LayerStatus({ status }: { status: LayerStatusValue }) {
  return <StatusChip status={status} testId="layer-status" />;
}
