import { Route, Routes, Navigate } from "react-router-dom";
import { AppLayout } from "./components/Layout";
import { Overview } from "./pages/Overview";
import { CodingProgressPage } from "./pages/CodingProgress";
import { ResearchContextPage } from "./pages/ResearchContextPage";
import { Documents } from "./pages/Documents";
import { DocumentDetail } from "./pages/DocumentDetail";
import { Segments } from "./pages/Segments";
import { SegmentDetail } from "./pages/SegmentDetail";
import { Coders } from "./pages/Coders";
import { CodebookPage } from "./pages/Codebook";
import { CodePage } from "./pages/Code";
import { ThemeCodingJobsPage } from "./pages/ThemeCodingJobs";
import { ThemeCodingJobNewPage } from "./pages/ThemeCodingJobNew";
import { ThemeCodingJobDetailPage } from "./pages/ThemeCodingJobDetail";
import { ThemesPage } from "./pages/Themes";
import { ThemeDetailPage } from "./pages/ThemeDetail";

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
        <Route path="/coding-progress" element={<CodingProgressPage />} />
        <Route path="/codebook" element={<CodebookPage />} />
        <Route path="/codebook/:version" element={<CodebookPage />} />
        <Route path="/code/:id" element={<CodePage />} />
        <Route path="/theme-coding-jobs" element={<ThemeCodingJobsPage />} />
        <Route
          path="/theme-coding-jobs/new"
          element={<ThemeCodingJobNewPage />}
        />
        <Route
          path="/theme-coding-jobs/:id"
          element={<ThemeCodingJobDetailPage />}
        />
        <Route path="/themes" element={<ThemesPage />} />
        <Route path="/themes/:id" element={<ThemeDetailPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppLayout>
  );
}
