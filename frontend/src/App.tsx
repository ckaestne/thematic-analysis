import { Route, Routes, Navigate } from "react-router-dom";
import { AppLayout } from "./components/Layout";
import { Overview } from "./pages/Overview";
import { ResearchContextPage } from "./pages/ResearchContextPage";
import { Documents } from "./pages/Documents";
import { DocumentDetail } from "./pages/DocumentDetail";
import { Segments } from "./pages/Segments";
import { SegmentDetail } from "./pages/SegmentDetail";
import { Coders } from "./pages/Coders";
import { CoderRuns } from "./pages/CoderRuns";
import { Aggregations } from "./pages/Aggregations";
import { ReviewDecisions } from "./pages/ReviewDecisions";
import { CodebookPage } from "./pages/Codebook";
import { ThemeCoders } from "./pages/ThemeCoders";
import { Themes } from "./pages/Themes";

export default function App() {
  return (
    <AppLayout>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/research-context" element={<ResearchContextPage />} />
        <Route path="/documents" element={<Documents />} />
        <Route path="/documents/:id" element={<DocumentDetail />} />
        <Route path="/segments" element={<Segments />} />
        <Route path="/segments/:id" element={<SegmentDetail />} />
        <Route path="/coders" element={<Coders />} />
        <Route path="/coder-runs" element={<CoderRuns />} />
        <Route path="/aggregations" element={<Aggregations />} />
        <Route path="/review-decisions" element={<ReviewDecisions />} />
        <Route path="/codebook" element={<CodebookPage />} />
        <Route path="/codebook/:version" element={<CodebookPage />} />
        <Route path="/theme-coders" element={<ThemeCoders />} />
        <Route path="/themes" element={<Themes />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppLayout>
  );
}
