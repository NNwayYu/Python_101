import streamlit as st
import pandas as pd

st.title("IT Inventory Dashboard")

# Load data
df = pd.read_excel("data.xlsx") 

df.index = range(1, len(df) + 1)
print(df)

# Show main numbers
st.metric("Total Servers", len(df))
st.metric("Servers Down", len(df[df["Status"] == "Down"]))

# Show table
st.subheader("Server Status")
st.dataframe(df)

# Simple chart
st.subheader("CPU Usage")
st.bar_chart(df.set_index("Server")["CPU"])
