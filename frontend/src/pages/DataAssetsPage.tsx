import { useParams } from "react-router-dom";
import { CatalogView } from "../features/data-assets/CatalogView";
import { DataAssetsLayout } from "../features/data-assets/DataAssetsLayout";
import "../features/data-assets/DataAssetsLayout.css";

/** Route dispatch only: content packages own their independent view modules. */
export function DataAssetsPage() {
  const { datasetId } = useParams();
  return datasetId ? <DataAssetsLayout key={datasetId} datasetId={datasetId} /> : <CatalogView />;
}
