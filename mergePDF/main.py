from PyPDF2 import PdfMerger

merger = PdfMerger() #create a PdfMerger object
merger.append("pdf/pdf-1.pdf") #append pdf files to the merger
merger.append("pdf/pdf-2.pdf")
merger.append("pdf/pdf-3.pdf")

merger.write("merged.pdf") #write out the merged PDF to a file
merger.close() #close the PdfMerger object


